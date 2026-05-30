"""``ctxr-fsm spec`` — validate and register FSM specifications.

The ``spec`` subcommand group is the operator-facing surface for
moving an :class:`~ctxr.fsm.FsmSpec` from a Python module into the
project's SQLite store. Three commands ship in W3:

* ``spec validate <import-path>`` — load the spec from a
  ``<module>:<attribute>`` import path and run
  :func:`~ctxr.fsm.core.spec.validate_fsm_spec`. The full
  :class:`~ctxr.fsm.core.spec.FsmValidationResult` is rendered for
  humans (rich-formatted breakdown) or scripts (``--json``).
* ``spec register <import-path> [--project-slug SLUG]`` — validates
  first (we refuse to persist an invalid spec), then delegates to
  :meth:`Project.register_spec`. Prints the resulting
  :class:`~ctxr.fsm.sqlite.repos_core.SpecRegistered` envelope so the
  caller can see whether a new version was minted or an existing one
  matched.
* ``spec list [--project-slug SLUG]`` — enumerates every registered
  spec in the project, grouped by ``slug``. When ``--project-slug`` is
  omitted, every project in the DB is walked; when supplied, listing
  is scoped to just that project (and we surface a clear error when
  the slug does not exist).

All three commands honour the standard ``--db`` / ``$CTXR_FSM_DB`` /
``./.ctxr-fsm/fsm.db`` precedence via :data:`DB_OPTION` and the
``--json`` toggle via :data:`JSON_OPTION`. Validation failures and
unknown-slug lookups exit non-zero so scripts can pipe through ``set
-e`` without writing a parser around the human output.

Why load specs from Python rather than YAML / JSON
--------------------------------------------------

The project's spec model is Python-first: workers, predicates,
response schemas are constructed inside Python so static typing
catches errors at author time. Persisting the canonical Python source
of truth (rather than a serialised intermediate) means register/list
preserves exactly what the author wrote, and re-loading a spec for
validation does not depend on a round-tripping serialiser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from ctxr.fsm import FsmSpec, FsmValidationResult
from ctxr.fsm.cli._common import (
    DB_OPTION,
    JSON_OPTION,
    die,
    json_or_pretty,
    load_spec,
    open_project_for_cli,
    resolve_db_path,
)
from ctxr.fsm.sqlite import Project, ProjectsRepo, RegisteredSpec, SpecsRepo

__all__ = ["spec_app"]


# ---------------------------------------------------------------------------
# Sub-app
# ---------------------------------------------------------------------------

# ``no_args_is_help=True`` keeps ``ctxr-fsm spec`` (no subcommand) helpful
# instead of silently doing nothing — matches the parent app's policy.
spec_app: typer.Typer = typer.Typer(
    name="spec",
    help="Validate and register FSM specs.",
    no_args_is_help=True,
    add_completion=False,
)


# ---------------------------------------------------------------------------
# Reusable argument / option definitions
# ---------------------------------------------------------------------------

# We pull these out as module-level constants so every subcommand's
# Typer signature is short and the help text is defined in exactly one
# place. ``typer.Argument`` / ``typer.Option`` are not type-strict at
# the call-site, so ``Any`` is the most honest annotation.
SPEC_IMPORT_PATH_ARG: Any = typer.Argument(
    ...,
    metavar="SPEC_IMPORT_PATH",
    help=(
        "Python import path of the form '<module>:<attribute>' that "
        "resolves to an FsmSpec instance "
        "(e.g. 'examples.plan_implement_qa_fix:spec')."
    ),
)


PROJECT_SLUG_OPTION: Any = typer.Option(
    None,
    "--project-slug",
    help=(
        "Project slug to register/list under. Defaults to 'default' "
        "for register; lists every project when omitted."
    ),
)


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


def _validation_payload(spec: FsmSpec, result: FsmValidationResult) -> dict[str, Any]:
    """Shape a :class:`FsmValidationResult` for ``--json`` output.

    We add the resolved spec id, version, and content hash so the JSON
    consumer can correlate the validation outcome with the spec it ran
    against without a second round-trip. ``dangling_transitions`` and
    ``invalid_predicates`` are tuples on the result; JSON has no tuple
    type so we convert to ``[from, to]`` / ``[location, expression]``
    lists for portability.
    """
    return {
        "spec_id": spec.id,
        "spec_version": spec.version,
        "spec_hash": spec.hash(),
        "valid": result.valid,
        "errors": list(result.errors),
        "unreachable_states": list(result.unreachable_states),
        "dangling_transitions": [list(pair) for pair in result.dangling_transitions],
        "invalid_predicates": [list(pair) for pair in result.invalid_predicates],
    }


def _render_validation_pretty(spec: FsmSpec, result: FsmValidationResult) -> None:
    """Render a :class:`FsmValidationResult` using rich.

    We split the output into a header (spec identity + verdict) and a
    body that only lists the diagnostic categories that have content.
    Suppressing empty sections keeps a clean "no problems found" run
    visually compact.
    """
    verdict = "[bold green]valid[/bold green]" if result.valid else "[bold red]invalid[/bold red]"
    from rich import print as rich_print

    rich_print(
        f"Spec [bold]{spec.id}[/bold] v{spec.version} "
        f"([dim]{spec.hash()[:12]}…[/dim]): {verdict}"
    )

    if result.errors:
        rich_print("[bold]Errors:[/bold]")
        for err in result.errors:
            rich_print(f"  [red]•[/red] {err}")

    if result.unreachable_states:
        rich_print("[bold]Unreachable states:[/bold]")
        for state_id in result.unreachable_states:
            rich_print(f"  [yellow]•[/yellow] {state_id}")

    if result.dangling_transitions:
        rich_print("[bold]Dangling transitions:[/bold]")
        for src, dst in result.dangling_transitions:
            rich_print(f"  [yellow]•[/yellow] {src} -> {dst}")

    if result.invalid_predicates:
        rich_print("[bold]Invalid predicates:[/bold]")
        for location, expression in result.invalid_predicates:
            rich_print(f"  [yellow]•[/yellow] {location}: {expression!r}")


def _spec_summary(spec: RegisteredSpec) -> dict[str, Any]:
    """Project a :class:`RegisteredSpec` into a JSON-safe summary dict.

    Used by ``register`` (single result) and ``list`` (list of
    results). We drop the heavyweight ``definition`` payload because
    callers who need it can fetch it via the API — the CLI's job is to
    surface identity and provenance.
    """
    return {
        "id": spec.id,
        "project_id": spec.project_id,
        "slug": spec.slug,
        "version": spec.version,
        "hash": spec.hash,
        "created_at": spec.created_at,
    }


def _group_specs_by_slug(specs: list[RegisteredSpec]) -> dict[str, list[dict[str, Any]]]:
    """Group a flat list of registered specs by their ``slug``.

    Within each slug the versions are sorted ascending so the consumer
    sees the natural chronological order. Slugs themselves are sorted
    alphabetically so the output is stable across runs (the upstream
    ``list_versions`` already returns ascending versions, but we sort
    defensively in case a future repo change reorders rows).
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for spec in specs:
        grouped.setdefault(spec.slug, []).append(_spec_summary(spec))
    for slug in grouped:
        grouped[slug].sort(key=lambda row: int(row["version"]))
    return dict(sorted(grouped.items()))


def _collect_distinct_slugs(project: Project, project_id: str) -> list[str]:
    """Return every distinct spec slug under ``project_id``.

    There is no first-class repo method for "list distinct slugs", so
    we issue a small raw-SQL query against ``fsm_specs``. The query is
    parameter-bound so there is no injection vector. We sort
    alphabetically to give the caller a stable iteration order.
    """
    from sqlalchemy import text

    with project.session_factory() as session:
        rows = session.execute(
            text(
                "SELECT DISTINCT slug FROM fsm_specs "
                "WHERE project_id = :pid ORDER BY slug ASC"
            ),
            {"pid": project_id},
        ).all()
    return [str(row[0]) for row in rows]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@spec_app.command(name="validate", help="Validate an FsmSpec without touching the DB.")
def validate(
    spec_import_path: str = SPEC_IMPORT_PATH_ARG,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Load ``SPEC_IMPORT_PATH`` and run the structural validator.

    The DB is never opened — this command is purely an in-memory check
    so authors can iterate on a spec without provisioning storage.
    Exits non-zero when validation fails so the command is safe to use
    as a pre-commit / CI gate.
    """
    spec = load_spec(spec_import_path)
    result = spec.validate()

    if json_mode:
        json_or_pretty(_validation_payload(spec, result), json_mode=True)
    else:
        _render_validation_pretty(spec, result)

    if not result.valid:
        # Use the helper so the failure shows up on stderr with a
        # consistent banner. We've already printed the structured
        # breakdown above, so the message here is intentionally short.
        die("spec validation failed", code=1)


@spec_app.command(name="register", help="Register an FsmSpec into the project DB.")
def register(
    spec_import_path: str = SPEC_IMPORT_PATH_ARG,
    db: Path | None = DB_OPTION,
    json_mode: bool = JSON_OPTION,
    project_slug: str | None = PROJECT_SLUG_OPTION,
) -> None:
    """Persist ``SPEC_IMPORT_PATH`` under ``--project-slug`` (default ``'default'``).

    We validate first because :meth:`SpecsRepo.register` will happily
    accept any well-formed :class:`FsmSpec` — content-hash dedup means
    a broken spec would still be written to disk. Refusing to persist
    invalid input keeps the database trustworthy as a downstream source
    of truth.

    On success we print the :class:`SpecRegistered` summary with
    ``created`` so the caller can tell whether a fresh version was
    minted or the call deduped against an existing row.
    """
    spec = load_spec(spec_import_path)
    result = spec.validate()
    if not result.valid:
        # Show the same breakdown the validate command emits so the
        # operator gets actionable feedback without having to re-run.
        if json_mode:
            json_or_pretty(_validation_payload(spec, result), json_mode=True)
        else:
            _render_validation_pretty(spec, result)
        die("refusing to register an invalid spec", code=1)

    slug = project_slug or "default"
    db_path = resolve_db_path(db)
    with open_project_for_cli(db_path) as project:
        registered = project.register_spec(spec, project_slug=slug)

    payload: dict[str, Any] = {
        "project_slug": slug,
        "created": registered.created,
        "spec": _spec_summary(registered.spec),
    }
    json_or_pretty(payload, json_mode)


@spec_app.command(name="list", help="List registered FSM specs in the project DB.")
def list_specs(
    db: Path | None = DB_OPTION,
    json_mode: bool = JSON_OPTION,
    project_slug: str | None = PROJECT_SLUG_OPTION,
) -> None:
    """Enumerate registered specs, grouped by slug.

    When ``--project-slug`` is omitted, every project's specs are
    surfaced; the JSON shape is ``{project_slug: {spec_slug:
    [versions...]}}`` so consumers can route on either axis. When the
    flag is supplied we restrict the walk to that single project — and
    we exit non-zero if the slug does not match a known project, so
    operators don't silently get an empty result from a typo.
    """
    db_path = resolve_db_path(db)
    projects_repo = ProjectsRepo()
    specs_repo = SpecsRepo()

    out: dict[str, dict[str, list[dict[str, Any]]]] = {}

    with open_project_for_cli(db_path) as project:
        with project.session_factory() as session:
            if project_slug is not None:
                target = projects_repo.get_by_slug(session, project_slug)
                if target is None:
                    die(f"no project registered with slug {project_slug!r}", code=1)
                projects_to_walk = [target]
            else:
                projects_to_walk = projects_repo.list(session)

        for proj in projects_to_walk:
            slugs = _collect_distinct_slugs(project, proj.id)
            collected: list[RegisteredSpec] = []
            with project.session_factory() as session:
                for slug in slugs:
                    collected.extend(specs_repo.list_versions(session, proj.id, slug))
            out[proj.slug] = _group_specs_by_slug(collected)

    json_or_pretty(out, json_mode)
