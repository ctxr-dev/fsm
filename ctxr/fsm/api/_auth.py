"""Bearer-token auth helpers for the HTTP API.

The API has two operating modes, distinguished by whether the
``CTXR_FSM_API_TOKEN`` environment variable is set at request time
(read on every call so tests can flip it between cases without a
restart):

* **Dev mode** — ``CTXR_FSM_API_TOKEN`` is unset/empty. Every request
  is trusted; the UI dev server on ``http://localhost:5173`` can hit
  the API without ceremony. The CORS allowlist is still enforced.
* **Production mode** — ``CTXR_FSM_API_TOKEN`` is set. Every request
  must present ``Authorization: Bearer <token>`` matching the env-var
  value byte-for-byte. Missing or mismatched headers respond with
  ``401`` (no header) or ``403`` (bad token), following the
  conventional split.

The helper :func:`check_authorization` is the single decision point;
the FastAPI dependency in :mod:`ctxr.fsm.api._deps` is a thin wrapper
that surfaces it to routes via ``Depends(require_auth)``. Keeping the
logic in a plain function — rather than only inside the dependency —
lets tests exercise the predicate directly without spinning up the
ASGI stack.

Why read the env var on every call rather than caching at import?
Two reasons. First, tests routinely toggle the token via
``monkeypatch.setenv`` between cases, and a cached read would force
those tests to reach into private state. Second, operators who rotate
the token by ``systemctl set-environment`` followed by ``systemctl
reload`` (no full restart) get the new value picked up on the next
request — the cost is a single ``os.environ.get`` per call, which is
free compared to the SQLite work each request does.
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, status

__all__ = ["AUTH_ENV_VAR", "check_authorization"]


# The environment variable whose presence flips the server into
# token-protected mode. Defined as a constant so the docs / tests /
# CLI shim all reference the same string instead of inlining the
# magic name in three places.
AUTH_ENV_VAR: str = "CTXR_FSM_API_TOKEN"


def _expected_token() -> str | None:
    """Return the configured token, or ``None`` if dev mode is active.

    Empty strings (``CTXR_FSM_API_TOKEN=``) are treated the same as
    unset — an operator who unsets the variable via ``unset
    CTXR_FSM_API_TOKEN`` and one who blanks it via ``CTXR_FSM_API_TOKEN=
    foo command`` (where the env was reset to empty by the shell)
    should both end up in dev mode, never in "all requests fail with
    403 because the token is the empty string".
    """
    value = os.environ.get(AUTH_ENV_VAR)
    if not value:
        return None
    return value


def check_authorization(authorization: str | None) -> None:
    """Raise ``HTTPException`` if ``authorization`` is invalid.

    In dev mode (no token configured) this is a no-op regardless of
    what the header carries — a client that *does* send a Bearer
    header is harmless, we simply ignore it.

    In production mode the header must:

    * Be present (otherwise ``401 Unauthorized`` with a
      ``WWW-Authenticate: Bearer`` challenge so clients know what to
      send next).
    * Start with the case-insensitive scheme ``Bearer`` followed by
      a single space and a non-empty token.
    * Carry a token whose bytes match the configured value under
      :func:`hmac.compare_digest` (constant-time comparison to deny
      timing side-channels — overkill for a local-dev token, but the
      same code path will eventually serve remote deployments).

    Any failure beyond the missing-header case responds with
    ``403 Forbidden`` rather than 401 — the client *did* present
    credentials, they were just wrong, which the RFC reserves for 403.
    """
    expected = _expected_token()
    if expected is None:
        # Dev mode — no enforcement. Returning silently here is the
        # whole point of the mode: contributors should be able to
        # ``curl http://localhost:8000/api/v1/...`` without setting up
        # a token.
        return

    if authorization is None or not authorization.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        # Header present but not parseable as Bearer — 403, not 401:
        # the client claimed credentials, they are just malformed.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authorization header must use the Bearer scheme.",
        )

    presented = parts[1].strip()
    # ``hmac.compare_digest`` rejects strings of different lengths
    # without leaking the length through timing — exactly what we want
    # for token comparison. Wrapped in ``encode()`` because the digest
    # comparison operates on bytes; both sides are ASCII tokens so the
    # UTF-8 default is fine.
    if not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid bearer token.",
        )
