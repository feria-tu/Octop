# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **`AGENTS.md` is the authoritative guide** for module boundaries, coding conventions, i18n, DB schema rules, and cross-platform test rules. Read the relevant section there before making changes; this file only summarizes the essentials and does not override it.

## What this is

**Octop** — a self-hosted, multi-user, multi-agent AI assistant platform. One Python wheel (`src/octop/`) ships a FastAPI backend, a React dashboard (built artifact), and a Click CLI. Single process: no external queue or required services beyond an LLM provider. All runtime state lives under `~/.octop/` (SQLite by default, PostgreSQL optional).

Built on the Octop Harness stack (`octop-harness`, `octop-gateway`, `octop-memory`, `octop-browser` — external packages). Octop composes them into one process that routes every surface (Web UI, IM channels, cron) through a single in-process `HarnessProcessor`.

## Commands

```bash
make install            # backend dev deps (uv sync)
make install-hooks      # once per clone — enables the pre-commit gate (required for commits)
make all                # SHIP BAR: format-all + lint + typecheck + test
make check-all          # full stack: lint-all + typecheck-all + test

make format-all         # backend ruff --fix/format + dashboard prettier write
make lint               # ruff check + format check (src, tests)
make typecheck          # mypy --strict src/octop
make test               # pytest -m "not live" (parallel)
make test-fast          # also excludes @slow
make precommit          # what the git hook runs: format-all + lint + typecheck + testmon-scoped tests

cd dashboard && npx tsc -b   # frontend typecheck (after UI changes)
make build-frontend          # dashboard/ -> src/octop/dashboard/ (do NOT edit the built copy)
```

Single test / targeted runs (always `uv run pytest`, never bare `pytest`):

```bash
uv run pytest tests/unit -x -q                  # unit only, stop on first failure
uv run pytest tests/integration -x -q
uv run pytest tests/unit/i18n -q                # required after any i18n JSON change
uv run pytest tests/unit/db/test_db_pool.py -q
uv run pytest path/to/test_file.py::test_name -q
```

- Pre-commit runs `make precommit` + `npm run build`. Emergency bypass only: `SKIP_PRECOMMIT=1 git commit` or `git commit --no-verify`. Never skip to land red tests; CI is the authoritative full-suite gate.
- CI runs on **Linux and Windows** — tests must be cross-platform (see AGENTS.md §7).

## Architecture

Dependencies flow **inward**; transport layers call domain, domain never calls HTTP/CLI.

```
dashboard/ ──HTTP──► api/ ──► infra/ ──► infra/utils/, octop.config
cli/ ──► launch.py ──► api/ + infra/
```

| Layer | Role |
|-------|------|
| `octop.config` | Env-based `OctopConfig` (stdlib + pydantic only) |
| `octop.i18n` | Locale JSON (`en`/`zh`) + `tr()` + domain helpers |
| `octop.launch` | Composition root: `OctopServer` + `build_app` + uvicorn. Only module allowed to import both `infra/server` and `api/app` |
| `infra/` | Business logic & orchestration (agents, gateway, cron, db, users, connectors, skills, backend, history, setup) |
| `infra/utils/`, `infra/db/repos/` | Leaf modules — pure helpers / one repo per table, SQL only |
| `api/` | FastAPI: `app.py` factory, `routers/` (thin — validate HTTP, call `infra/`, map errors), `deps.py`, `middleware/` |
| `cli/` | Click entry (`main.py`) + `*_cmd.py`. Transport per command: **offline** (local SQLite), **embedded** (boots in-process `OctopServer`), or **external** (OS/daemon) |
| `dashboard/` | React + TS + Vite source (built to `src/octop/dashboard/`) |

**Hard bans** (enforced by convention): `infra/` must not import `api/`, `cli/`, or `launch.py`; `api/` must not import `cli/` or `launch.py`; `cli/` must not import `api/`; repos/utils must not import other domain `infra` packages. Never import legacy top-level modules (`octop.agents.*`, `octop.db.*`, …) — use `octop.infra.*`.

### Key patterns

- **DI:** routers take `server: OctopServer = Depends(get_server)`; services live on `server.services` (`SharedServices` from `infra/db/services.py`). Never import repos at module level outside `SharedServices`/`RepoBundle`.
- **Agent scope:** most agent routes are `/api/agents/{agent_id}/…`; validate ownership via the agent row + `_assert_agent_owner(row, user)` (admin bypass).
- **Agent workspace I/O:** all content-file reads/writes go through `HarnessAgent.workspace` (`BackendWorkspace` from `octop-harness`). Do not use `agent.backend`, `resolve_harness_backend`, or `Path.write_text` on `~/.octop/agents/<id>/`, and do not branch on backend type in Octop. Pass workspace-relative paths (`SOUL.md`); `"."` is the current dir, `"/"` is the backend root.
- **Database:** SQLite and PostgreSQL share one schema. Add/change tables via a numbered pair `infra/db/migrations/00N_*.sql` **and** `00N_*.pg.sql`, then bump the version assertion in `tests/unit/db/test_db_pool.py`. Unreleased schema work folds into the current unreleased `00N`, not a new number.
- **i18n:** user-facing text from the **server** (API errors, slash replies, IM status, tool names, CLI output) must come from backend bundles (`src/octop/i18n/en.json` + `zh.json`), not hard-coded English in `infra/`. Keep `en`/`zh` key trees identical; dashboard `apiErrors.*` must mirror backend `errors.*`.
- **Timezone:** user-facing datetimes and cron defaults use the server timezone (`default_timezone` in `config.json`, via `GET /api/settings/timezone`), never the browser's. Use the dashboard `formatServerDateTime` helpers.
- **API docs:** `/api/docs` (Scalar). Give routes a `summary`/`description`, use typed `response_model`, and reuse tags from `api/openapi_meta.py`.

## Workflow

Feature work: branch from `develop`, PR **into `develop`**. Never push `develop` → `main` directly; releases go `release/x.y.z` → `main` → tag `v*` on main tip. Release flow: `.cursor/skills/publish` (`/publish <version>`). Full rules: `CONTRIBUTING.md` and AGENTS.md §10.

Before saying "done": run `make all` (and `npx tsc -b` after `dashboard/` changes), and cite the result. Do not commit or push unless asked.
