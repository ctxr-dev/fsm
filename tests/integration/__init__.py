"""Integration tests for ctxr.fsm.

Tests in this package exercise the substrate end-to-end through public
entry points (CLI, MCP server, HTTP API) rather than calling the
internal Python API directly. They are deliberately slower than the
unit tests under ``tests/unit/`` — expect seconds, not milliseconds —
because each test spins up the real subprocess / transport stack.

Sub-packages:

* :mod:`tests.integration.sqlite` — end-to-end lifecycle exercises
  that touch the SQLite repositories through ``Project``.
* :mod:`tests.integration.mcp` — MCP server handshake / tool surface
  tests; spawn ``ctxr-fsm mcp`` as a subprocess and drive it over the
  stdio JSON-RPC transport using the official ``mcp`` SDK client.
"""
