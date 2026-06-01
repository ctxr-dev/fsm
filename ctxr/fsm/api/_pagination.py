"""``Page[T]`` envelope + pagination helpers for the HTTP list surface.

The pre-W22b2 list endpoints returned raw ``list[T]`` with at best a
``limit``/``offset`` query pair and no envelope. That shape is fine
for ad-hoc curl debugging but fails the dashboard the moment a list
crosses a single screen: the UI cannot show "page 4 of 12", cannot
jump to the last page, cannot reason about whether more rows exist
without re-issuing the request with a bumped offset and inspecting
the length.

This module ships:

* :class:`Page` — Pydantic generic envelope, ``{items, page,
  page_size, total, has_next, sort}``. ``total`` is computed in the
  same round-trip via SQL's ``COUNT(*) OVER ()`` window function, so
  the wire format always exposes the population size without a second
  query. ``has_next`` is derived rather than transmitted, so a client
  that loses or strips it can still compute the value.
* :class:`PageParams` — the FastAPI ``Depends()``-friendly query
  bundle (``page``, ``page_size``, ``sort``). Routes take a single
  ``page_params: PageParamsDep`` annotation instead of three loose
  ``Query()`` parameters; this keeps the OpenAPI schema clean and
  makes route signatures grep-able for "which endpoints paginate".
* :func:`paginate_sa_select` — for endpoints whose handler builds a
  raw SQLAlchemy ``select()``. Bolts the window-function count onto
  the query, applies ``OFFSET`` / ``LIMIT``, executes once, and
  unpacks rows + total into a :class:`Page`. Used by the four inline-
  SQL handlers in ``routes_admin.py`` and ``routes_specs.py``.
* :func:`paginate_sequence` — for endpoints whose repository method
  still returns a Python list (legacy backward-compat path during
  rollout). Slices in-memory + counts via ``len()``. Marked deprecated
  in the docstring; new code MUST take the SA path.

Why a window-function count, not a separate ``COUNT(*)`` query?
SQLite's WAL handles read concurrency, but two separate queries can
return inconsistent ``total`` vs. ``items`` lengths under a concurrent
write (a row appears between the two queries). The window function
runs the count in the same statement as the row fetch, so the
serialisable view is identical for both. Cost is one extra integer
column per row in the result set, which is negligible for the page
sizes the UI uses (default 50, max 200).

Why no ``next_cursor`` / ``prev_cursor``? The user's explicit
requirement is "pagination with most-recent-first sort on all lists
(runs, specs, versions, journals)" — operator-grade offset
pagination, not infinite scroll. Cursor pagination is the right
shape for streaming-heavy lists (events, tool_calls) when they get
unwieldy on long runs; that lands as a separate ``?cursor=`` opt-in
later, never as a replacement.

Sort direction
--------------

Each endpoint declares its own ``default_sort`` to the
:class:`PageParams` factory (e.g. ``runs`` uses ``last_update_at
DESC``, ``events`` uses ``seq ASC``). The user-supplied ``?sort=``
overrides; an unrecognised sort key returns 422 with the allowed
values, so the wire contract surfaces validation rather than
silently falling back.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Self

from fastapi import Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, func, over
from sqlalchemy.engine import Connection
from sqlalchemy.sql import expression

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "Page",
    "PageParams",
    "PageParamsDep",
    "make_page_params",
    "paginate_sa_select",
    "paginate_sequence",
]


# --- limits ---------------------------------------------------------------
#
# Default page size matches the existing ``runs.tsx`` ``PAGE_SIZE`` magic
# constant (20 was the prior value, 50 is the post-W22b2 default the user
# requested). Max page size caps how much a malicious client can request
# in one shot — 200 rows per page is enough for any operator workflow
# (anything past that wants a CLI, not a UI page).
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


# --- envelope -------------------------------------------------------------


class Page[T](BaseModel):
    """Generic page envelope for every list endpoint.

    Fields:

    * ``items`` — the page slice, already ordered per ``sort``.
    * ``page`` — 1-indexed page number that was served. 1 means the
      first page; a request with ``page > ceil(total / page_size)``
      returns an empty ``items`` array (rather than 404) so the UI
      can render "page N has no rows" without a branch.
    * ``page_size`` — server-clamped page size that was applied (the
      client's request is clamped to ``[1, MAX_PAGE_SIZE]``).
    * ``total`` — total row count BEFORE the slice. Derived in the
      same round-trip via ``COUNT(*) OVER ()`` so it is consistent
      with ``items``.
    * ``has_next`` — true when ``page * page_size < total``. A
      derived field; clients that need ``prev`` compute it the same
      way (``page > 1``).
    * ``sort`` — the sort key actually applied (after validation
      against the endpoint's allow-list). Exposed so the UI can show
      "Sorted by: last_update_at desc" without guessing.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    items: list[T]
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=MAX_PAGE_SIZE)
    total: int = Field(..., ge=0)
    has_next: bool
    sort: str

    @classmethod
    def empty(cls, *, page: int, page_size: int, sort: str) -> Self:
        """Build an empty page (no rows, total=0) — useful in 0-row
        early-return branches that still need the envelope shape.
        """
        return cls(
            items=[],
            page=page,
            page_size=page_size,
            total=0,
            has_next=False,
            sort=sort,
        )


# --- params ---------------------------------------------------------------


class PageParams(BaseModel):
    """The validated ``?page=&page_size=&sort=`` bundle.

    Constructed via :func:`make_page_params`, which lets each route
    declare its own ``default_sort`` and ``allowed_sorts``. The
    raw class isn't useful directly — every route needs the
    allow-list to give 422 on unknown sort keys.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=MAX_PAGE_SIZE)
    sort: str

    @property
    def offset(self) -> int:
        """Compute the SQL ``OFFSET`` from the 1-indexed ``page``.

        ``page=1`` yields offset 0; ``page=3, page_size=50`` yields
        offset 100. Centralised here so individual routes can't drift
        on the off-by-one boundary.
        """
        return (self.page - 1) * self.page_size


def make_page_params(
    *,
    default_sort: str,
    allowed_sorts: tuple[str, ...],
) -> type[PageParams]:
    """Build a route-specific ``PageParams`` factory.

    Returns a FastAPI dependency callable that captures
    ``default_sort`` + ``allowed_sorts`` and validates the
    user-supplied ``?sort=`` against the allow-list. An unknown sort
    key triggers a 422 with the allowed values, exposing the contract
    to the client rather than silently coercing to the default.

    Each route module declares its own factory at import time, e.g.::

        RunsPageParams = make_page_params(
            default_sort="last_update_at_desc",
            allowed_sorts=("last_update_at_desc", "started_at_desc", "started_at_asc"),
        )

    and binds it via::

        @router.get(..., dependencies=[Depends(require_auth)])
        async def list_runs(
            params: Annotated[PageParams, Depends(RunsPageParams)],
            ...
        ): ...

    The returned callable is NOT a ``PageParams`` subclass — it's a
    factory that produces validated ``PageParams`` instances. Pydantic
    validation happens inside the factory rather than via class-level
    ``Field`` constraints because the allow-list is per-route.
    """
    if default_sort not in allowed_sorts:
        raise ValueError(
            f"default_sort {default_sort!r} not in allowed_sorts {allowed_sorts!r}"
        )

    # The factory below uses the legacy ``param: type = Query(...)``
    # FastAPI signature style rather than the modern
    # ``param: Annotated[type, Query(...)]`` form. The Annotated form
    # interacts badly with ``from __future__ import annotations``
    # because the closure-captured ``default_sort`` / ``allowed_sorts``
    # become unresolvable forward refs when Pydantic's TypeAdapter
    # tries to validate the parameter shape. The legacy form sidesteps
    # the issue: defaults are concrete values, not stringified
    # annotation literals. FastAPI accepts both forms equivalently for
    # query-parameter binding + OpenAPI generation.
    _description = (
        f"Sort key. Allowed: {', '.join(allowed_sorts)}. "
        f"Default: {default_sort}."
    )

    def _factory(
        page: int = Query(default=1, ge=1, description="1-indexed page number."),
        page_size: int = Query(
            default=DEFAULT_PAGE_SIZE,
            ge=1,
            le=MAX_PAGE_SIZE,
            description=f"Page size (max {MAX_PAGE_SIZE}).",
        ),
        sort: str = Query(default=default_sort, description=_description),
    ) -> PageParams:
        if sort not in allowed_sorts:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "unknown_sort",
                    "supplied": sort,
                    "allowed": list(allowed_sorts),
                },
            )
        return PageParams(page=page, page_size=page_size, sort=sort)

    # Stash the configuration on the factory so OpenAPI tooling /
    # tests can introspect what an endpoint accepts without having to
    # call the factory.
    _factory.default_sort = default_sort  # type: ignore[attr-defined]
    _factory.allowed_sorts = allowed_sorts  # type: ignore[attr-defined]
    return _factory  # type: ignore[return-value]


# Type alias for the rare cases where a route can accept ANY sort
# (e.g. a future search endpoint). Most routes use ``make_page_params``
# above; this alias is here for completeness, not as the primary path.
PageParamsDep = Annotated[PageParams, Depends(make_page_params(
    default_sort="id_asc",
    allowed_sorts=("id_asc", "id_desc"),
))]


# --- helpers --------------------------------------------------------------


def paginate_sa_select[T](
    conn: Connection,
    base_select: Select[Any],
    *,
    params: PageParams,
    row_factory: Callable[[Any], T],
) -> Page[T]:
    """Apply pagination to a SQLAlchemy ``select()`` + execute once.

    ``base_select`` MUST already have its ``order_by`` clause
    applied — the caller owns ordering because the sort key set
    differs per endpoint (and per ``PageParams.sort``). This helper
    is intentionally agnostic about column names.

    The window-function trick: we append a synthetic
    ``COUNT(*) OVER ()`` column to the select, run the paginated
    fetch, and unpack the count from any row. If the page is empty
    (no rows matched the filter, or ``page`` is past the last page)
    we issue a single ``SELECT COUNT(*) FROM (base)`` as a fallback
    to recover the total. That fallback path is cold for typical
    queries — the first read fills total in one round-trip.

    ``row_factory`` is invoked per row with the SQLAlchemy
    ``Row._mapping`` and is expected to return the Pydantic model
    the endpoint serialises. Passed in (not imported here) so this
    module stays decoupled from any specific model.

    Returns a fully-populated :class:`Page`.
    """
    count_col = over(func.count()).label("__page_total__")
    paged = (
        base_select.add_columns(count_col)
        .offset(params.offset)
        .limit(params.page_size)
    )
    rows = list(conn.execute(paged))
    if rows:
        total = int(rows[0]._mapping["__page_total__"])
        items: list[T] = [row_factory(row._mapping) for row in rows]
    else:
        # Empty page — either the table is empty OR we asked for a
        # page past the last one. Recover total via a separate count
        # so the envelope's ``total`` field stays honest.
        count_stmt = expression.select(func.count()).select_from(
            base_select.subquery()
        )
        total = int(conn.execute(count_stmt).scalar_one())
        items = []
    return Page(
        items=items,
        page=params.page,
        page_size=params.page_size,
        total=total,
        has_next=params.page * params.page_size < total,
        sort=params.sort,
    )


def paginate_sequence[T](seq: list[T], *, params: PageParams) -> Page[T]:
    """Slice a Python list into a :class:`Page` envelope.

    Last-resort path for repository methods that haven't been
    migrated to SA select-based pagination yet. The full sequence is
    materialised in memory before slicing, which scales poorly past
    a few thousand rows — every consumer SHOULD migrate to
    :func:`paginate_sa_select`. This helper exists so the wire
    format flips in lockstep across all endpoints in W22b2 without
    forcing every repository method to be rewritten in the same PR.
    """
    total = len(seq)
    offset = params.offset
    items = seq[offset : offset + params.page_size]
    return Page(
        items=items,
        page=params.page,
        page_size=params.page_size,
        total=total,
        has_next=offset + len(items) < total,
        sort=params.sort,
    )
