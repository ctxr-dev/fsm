"""Smoke tests for the runnable scripts under ``examples/``.

Each test in this package launches one of the example scripts as a
subprocess (``uv``'s resolved Python interpreter), waits for it to
exit, and asserts that the run reached its expected terminal state.

The examples themselves are documented in ``examples/README.md``.
"""
