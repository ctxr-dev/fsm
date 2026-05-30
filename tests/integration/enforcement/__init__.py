"""W12 enforcement-layer integration tests.

Covers the cross-cutting enforcement primitives wired by W12:

* spec-hash lock (layer 9) — re-registering the FSM spec under an
  in-flight run causes ``fsm.commit_outputs`` / ``fsm.get_brief`` to
  reject with ``fsm_spec_changed``.
* commit cosignature (layer 5) — when the state declares
  ``allowed_tools`` / ``verifier`` (or the env var forces strict
  mode), a valid signature is required; mismatched signatures emit
  ``commit_signature_mismatch``; valid ones land on
  ``commit_signatures`` and emit ``commit_signature_verified``.

All tests in this package drive the MCP tool functions in-process
(no subprocess spawn) — the surface they protect is the one MCP
clients use, but the body itself is a plain Python callable so we
keep the runtime cost ~ms per case.
"""
