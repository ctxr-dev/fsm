"""Integration tests for the ``ctxr-fsm serve`` lifecycle supervisor (W7).

These tests spawn the real ``ctxr-fsm serve`` Typer command as a
subprocess and observe its operator-facing contract end-to-end:

* The boot banner ``[ctxr-fsm supervisor] booted: ...`` lands on
  stderr inside a bounded wall-clock budget.
* The per-subsystem pid files under ``.ctxr-fsm/pids/`` are created
  with the supervisor's child pid and remain alive for the duration
  of the run.
* A second supervisor invocation against the same ``project_root``
  observes the existing singletons and skips its own spawn (logging
  ``... already running (pid=..., url=...); skipping spawn.``)
  instead of fighting the first supervisor for the same ports.

The sibling unit suite under ``tests/unit/lifecycle/`` covers the
primitives in isolation (port picking, singleton reuse vs replace,
release semantics). These integration tests confirm those primitives
are wired correctly when the supervisor drives them as a single
process tree — a contract no in-process test can prove on its own.
"""
