"""Integration tests for ``ctxr.fsm.api`` — the FastAPI HTTP/SSE surface.

These tests boot the FastAPI app against a real SQLite-backed
:class:`Project` (one per test, in a :class:`tempfile.TemporaryDirectory`)
and exercise the HTTP endpoints through
:class:`fastapi.testclient.TestClient` for synchronous routes or a
programmatic :class:`uvicorn.Server` for the SSE streams that
``TestClient`` cannot drive cleanly.

The integration package mirrors the layout of
``tests/integration/mcp/`` so a contributor familiar with one layer can
navigate the other without surprise.
"""
