# fsm/

Two roots cohabiting during the JavaScript → Python migration:

- **`./` (this root)** — the new `ctxr-fsm` Python 3.12+ library, with SQLite as the primary persistence layer, plus shipped subsystems: `ctxr.fsm.core`, `ctxr.fsm.sqlite`, `ctxr.fsm.mcp` (MCP server), `ctxr.fsm.api` (FastAPI), `fsm-ui` (Vite + Preact + Tailwind v4 SPA at `ui/`), and runnable agent-orchestration examples under `examples/`. Imported as `ctxr.fsm` via PEP 420 namespace packages (no top-level `ctxr/__init__.py`). PyPI distribution: `ctxr-fsm`. Not yet published — `0.1.0` is in active build.
- **`./legacy-js/`** — the existing published `@ctxr/fsm` Node.js library (251 tests, A1–A9 + A7 atomic-tx journal, 0.2.0-pre). Still publishes from there via `legacy-js/.github/workflows/publish.yml` so `skill-code-review` and every other npm consumer keeps working unchanged during the rewrite. Migrating JS consumers to the new Python system is a separate plan; until that lands, the JS root is the source of truth for shipped behaviour.

The full plan, including all locked decisions, sequenced workstreams (W0–W12), and verification gates, lives at `/Users/developer/.claude/plans/how-it-fits-toasty-gray.md` in the developer workspace.

## Status

| Subsystem | State | Path |
|---|---|---|
| `legacy-js/` Node `@ctxr/fsm` | Published, frozen feature set (bug-fix only) | `legacy-js/` |
| `ctxr.fsm.core` (Pydantic + engine) | In build (W1) | `ctxr/fsm/core/` |
| `ctxr.fsm.sqlite` (schema + repo) | In build (W2) | `ctxr/fsm/sqlite/` |
| `ctxr.fsm.cli` (typer) | In build (W3) | `ctxr/fsm/cli/` |
| `ctxr.fsm.mcp` (MCP server) | In build (W4) | `ctxr/fsm/mcp/` |
| `ctxr.fsm.api` (FastAPI) | In build (W5) | `ctxr/fsm/api/` |
| `fsm-ui` (Vite + Preact + Tailwind v4) | In build (W6) | `ui/` |
| Service lifecycle (supervisor) | In build (W7) | `ctxr/fsm/cli/serve.py` |
| Examples | In build (W8) | `examples/` |
| Principles + memory injection | In build (W11) | `ctxr/fsm/memory/` |
| Enforcement primitives | In build (W12) | across W1/W2/W4/W7 |

## Quick start (once W0 + W1 + W2 land)

```bash
# In your consumer project:
uv add ctxr-fsm                      # or: pip install ctxr-fsm
ctxr-fsm init                        # creates .ctxr-fsm/fsm.db + applies migrations + patches CLAUDE.md/AGENTS.md
ctxr-fsm serve --mode dev            # boots MCP + FastAPI + fsm-ui dev server
# browse http://localhost:<port>     # see the run dashboard
```

## Legacy quick start (npm)

```bash
cd legacy-js && npm install
npx @ctxr/fsm --help
```

See `legacy-js/README.md` for the JS API surface and CLI reference.

## Migration plan

The plan file (`how-it-fits-toasty-gray.md` in the developer workspace) is the single source of truth for what lands when. The high-level sequence:

```
W0  cohabitation       (you are here)
W1  core lib           Pydantic + engine + predicates + loop + aggregator
W2  sqlite             schema + alembic + repo + event bus + transactions
W3  CLI                typer app + init + runs + spec + serve
W4  MCP server         fsm.* tools incl. healthcheck + observe + confirm
W5  FastAPI            REST + SSE
W6  fsm-ui             Vite + Preact + Tailwind v4 SPA
W7  service lifecycle  supervisor + ports + PIDs + reuse + live reload
W8  examples           three runnable agent workflows
W11 principles + install-memory CLI
W12 enforcement        spec-hash lock + cosignature + verifier + drift detector + hook
W9  documentation
W10 CI/CD (manual publish only, per Principle 2)
```

## Contributing

This is part of the [ctxr-dev workspace](https://github.com/ctxr-dev). New Python work lands under `ctxr/fsm/` and is driven by per-workstream branches and PRs. JS bug fixes land under `legacy-js/`. Cross-cutting changes (the two-roots layout itself) live at this top level.

## License

MIT for both roots. See `LICENSE` (Python) and `legacy-js/LICENSE` (JS).
