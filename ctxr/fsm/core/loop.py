"""Loop iteration mechanics for the ctxr FSM core.

This module owns the per-iteration decision logic for a state that has a
:class:`~ctxr.fsm.core.models.Loop` attached, and the convention for
where iteration outputs land on disk (or in the worker artefact tree).

It is deliberately tiny and dependency-free beyond
``ctxr.fsm.core.models``: the loop runtime above this layer (in W3+)
calls :func:`decide` after each iteration to learn whether to keep
looping or stop, and calls :func:`outputs_path_for` to know where to
stage the iteration's outputs JSON.

Design notes
------------

* ``decide`` is pure: same inputs, same output. No I/O, no clock.
* ``outputs_path_for`` returns a POSIX-style relative path. We
  deliberately do NOT touch :mod:`pathlib` here so the result is stable
  across operating systems and safe to embed in manifests, briefs, and
  log lines without surprise normalisation.
* If a user provides ``Loop.iteration_outputs_dir``, we validate it as
  a safe relative path before using it. Anything that looks like an
  attempt to escape the workers tree (``..``, absolute path, drive
  letter, backslash) raises :class:`LoopConfigError` immediately rather
  than letting an unsafe path leak through to the filesystem.
"""

from __future__ import annotations

from typing import Any

from ctxr.fsm.core.models import Loop, LoopDecision, State

__all__ = ["LoopConfigError", "decide", "outputs_path_for"]


class LoopConfigError(ValueError):
    """Raised when a :class:`Loop` is configured with an unsafe value.

    Currently the only configuration validated by this module is
    :attr:`Loop.iteration_outputs_dir`, which must be a safe relative
    POSIX path (no leading ``/``, no backslashes, no ``..`` segments,
    no drive-letter / colon segments). Spec-time validation
    (non-empty strings, ``max_iterations >= 1``, etc.) is handled by
    Pydantic in :mod:`ctxr.fsm.core.models` and is out of scope here.
    """


# ---------------------------------------------------------------------------
# Loop iteration decision
# ---------------------------------------------------------------------------


def decide(
    state: State,
    outputs: dict[str, Any],
    iteration_n: int,
) -> LoopDecision:
    """Decide whether a loop body should continue or terminate.

    Parameters
    ----------
    state:
        The current FSM state. If ``state.loop`` is ``None`` the state
        is not a looping state and the returned decision has
        ``is_loop=False``.
    outputs:
        The structured outputs produced by the most recent iteration of
        the loop body's worker. The value at ``state.loop.done_field``
        is consulted; any value other than the boolean literal ``True``
        is treated as "not done" (no truthiness coercion).
    iteration_n:
        The 1-based index of the iteration that just completed. When
        this reaches :attr:`Loop.max_iterations` the loop terminates
        with ``reason="max_iterations"`` regardless of the done-field.

    Returns
    -------
    LoopDecision
        A :class:`LoopDecision` describing whether this is a loop at
        all, whether to terminate, the reason (if terminating), and
        the iteration index just observed.

    Notes
    -----
    The ``done_field`` check takes precedence over the
    ``max_iterations`` check: if the worker reports done on the final
    allowed iteration, the recorded reason is ``"done_field"`` (a
    successful completion), not ``"max_iterated"``.
    """
    loop: Loop | None = state.loop
    if loop is None:
        return LoopDecision(is_loop=False)

    if outputs.get(loop.done_field) is True:
        return LoopDecision(
            is_loop=True,
            terminate=True,
            reason="done_field",
            iteration_n=iteration_n,
        )

    if iteration_n >= loop.max_iterations:
        return LoopDecision(
            is_loop=True,
            terminate=True,
            reason="max_iterations",
            iteration_n=iteration_n,
        )

    return LoopDecision(
        is_loop=True,
        terminate=False,
        iteration_n=iteration_n,
    )


# ---------------------------------------------------------------------------
# Iteration outputs path
# ---------------------------------------------------------------------------


def _validate_iteration_dir(raw: str) -> str:
    """Validate and normalise a user-supplied ``iteration_outputs_dir``.

    Returns the directory with any trailing ``/`` stripped. Raises
    :class:`LoopConfigError` if the value contains unsafe path syntax.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise LoopConfigError("iteration_outputs_dir must be a non-empty string")

    candidate = raw.strip()

    if candidate.startswith("/"):
        raise LoopConfigError(
            f"iteration_outputs_dir must be a relative path, got {raw!r}"
        )
    if "\\" in candidate:
        raise LoopConfigError(
            f"iteration_outputs_dir must use POSIX separators, got {raw!r}"
        )

    # Strip a single trailing slash for consistent joining downstream;
    # we keep any leading "./" because it's a no-op in POSIX joins and
    # users sometimes write it explicitly.
    if candidate.endswith("/"):
        candidate = candidate[:-1]

    segments = candidate.split("/")
    for seg in segments:
        if seg == "..":
            raise LoopConfigError(
                f"iteration_outputs_dir must not contain '..' segments, got {raw!r}"
            )
        if ":" in seg:
            raise LoopConfigError(
                f"iteration_outputs_dir must not contain ':' in any segment, got {raw!r}"
            )

    return candidate


def outputs_path_for(state: State, iteration_n: int) -> str:
    """Return the POSIX path where iteration ``iteration_n`` is staged.

    The shape of the returned path is::

        workers/<dir>/iter-<N>.json

    where ``<dir>`` is either :attr:`Loop.iteration_outputs_dir` (when
    the FSM author has set it explicitly) or a derived default of the
    form ``"<state.id>-iters"``.

    Parameters
    ----------
    state:
        The current state. Must have ``state.loop`` set; if the state
        is not a loop state a :class:`LoopConfigError` is raised since
        there is no meaningful iteration-output path to compute.
    iteration_n:
        The 1-based iteration index.

    Raises
    ------
    LoopConfigError
        If ``state.loop`` is ``None`` or if
        ``state.loop.iteration_outputs_dir`` is configured but fails the
        safe-relative-path check (no leading ``/``, no ``\\``,
        no ``..``, no ``:`` segments).
    """
    loop: Loop | None = state.loop
    if loop is None:
        raise LoopConfigError(
            f"state {state.id!r} has no loop; cannot compute iteration outputs path"
        )

    if loop.iteration_outputs_dir is not None:
        directory = _validate_iteration_dir(loop.iteration_outputs_dir)
    else:
        directory = f"{state.id}-iters"

    return f"workers/{directory}/iter-{iteration_n}.json"
