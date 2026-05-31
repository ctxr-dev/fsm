"""Integration tests for the W4 ``ctxr-fsm mcp`` server.

These tests spawn ``ctxr-fsm mcp`` as a real subprocess and drive it
through the official ``mcp`` Python SDK's stdio client. They cover the
wire-level behaviour of every ``fsm.*`` tool — the equivalent unit
tests live next door under ``tests/unit/mcp`` and exercise the tool
bodies in-process; these integration tests confirm the JSON-RPC framing,
FastMCP registration, and stdio plumbing all line up end-to-end.

Each test gets an isolated ``tempfile.TemporaryDirectory`` SQLite path
so there is no cross-test contamination. Tests are intentionally slower
than the unit suite (subprocess spawn + MCP initialize handshake) — a
single tool round-trip is in the 5-15s range — so they should run
sparingly in the inner dev loop and always in CI.
"""
