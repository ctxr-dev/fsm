"""Public testing utilities for ctxr-fsm.

Shipped as part of the ctxr-fsm wheel so the same helpers the fsm
package's own E2E tests use are available to consumers writing their
own integration suites.

Public API:

``materialise_fixture_project(dest)``
    Copy the bundled fixture project (see ``fixture_project/README.md``)
    into ``dest``, resolve ``gitignore.template`` to ``.gitignore``,
    and seed two commits so the contents are diffable. Returns the
    project root path.

``drive_run_to_completion(project, spec_id, args, worker_outputs)``
    Drive a run start-to-completion via
    :meth:`ctxr.fsm.sqlite.project.Project.commit_and_advance`, looking
    up the worker output to commit at each worker state from the
    caller-supplied ``worker_outputs`` mapping. Returns a dict
    summarising the run's terminal state, visited states, and final
    DB-side counts.
"""

from __future__ import annotations

from ctxr.fsm.testing.fixture_project_materialiser import (
    materialise_fixture_project,
)
from ctxr.fsm.testing.run_driver import (
    DriverResult,
    drive_run_to_completion,
)

__all__ = [
    "DriverResult",
    "drive_run_to_completion",
    "materialise_fixture_project",
]
