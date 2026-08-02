# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- **Wire the pre-commit hook before committing:** `git config core.hooksPath scripts/hooks`. It is local git config, so a fresh clone silently skips the guard that keeps `docs/ARCHITECTURE.html` in sync with the specs it renders. Worktrees share the clone's setting. Contract and regeneration steps: [`docs/architecture-regeneration.md`](docs/architecture-regeneration.md); hook tests: `scripts/hooks/tests/test-pre-commit.sh`.
- **Edit the specs, never `docs/ARCHITECTURE.html` directly.** The page is a rendering of every spec under `docs/superpowers/specs/` and carries one `source-sha256` manifest line per spec.
- Per-subsystem setup lives in [`README.md`](README.md); the integration suite's rationale lives in [`integration/README.md`](integration/README.md).

## Gates

PRs are gated by `.github/workflows/tests.yml` (pytest, jest, tsc, web-bundle smoke, pgTAP), `.github/workflows/lint.yml` (ruff check, ruff format, mypy, eslint), and `.github/workflows/hooks.yml` (the pre-commit hook's own suite plus a re-verification of the `docs/ARCHITECTURE.html` manifest).
README's "Lint, format, and type-check" section has the exact local commands.
Run them before proposing a change is done; `uv run --locked` means a dependency edit must be followed by `uv lock` in that package.

## Test surfaces

The tests here span several surfaces run by different tools.
`.github/workflows/tests.yml` is the authoritative list of commands and their
working directories - read it before inventing one.

- `firmware/`, `pipeline/`, `integration/` - `uv run --locked --extra dev pytest`.
  All three carry a `uv.lock` and CI passes `--locked`, so dependency changes
  must go through `uv`, never bare `pip`.
- `app/` - `npx tsc --noEmit`, `npx jest --ci`, plus an `expo export` web-bundle
  smoke check that catches native-only modules imported at module top level.
- `supabase/tests/database/` - pgTAP, run with `supabase test db`. This is the
  database contract (RLS policies, table grants, storage bucket policies) that
  every other subsystem codes against. `supabase test db` creates the `pgtap`
  extension itself, so no migration declares it and a plain `supabase start` is
  enough to run the suites.

The `integration` and `app` jobs need a live stack. `supabase status -o env`
emits `API_URL`/`ANON_KEY`/`SERVICE_ROLE_KEY`, which the suites read as
`SUPABASE_URL`/`SUPABASE_ANON_KEY`/`SUPABASE_SERVICE_ROLE_KEY` - tests.yml shows
the mapping.

## Local Supabase

`supabase/config.toml` runs a deliberately trimmed stack: `studio`, `analytics`,
`edge_runtime`, and `storage.vector` are off, each with a comment saying why.
`realtime`, `storage`, and `auth` are load-bearing - `realtime` in particular
backs `useDeviceStatus`, and `device_status` is in the `supabase_realtime`
publication via migration. Do not disable those.

The CLI version is pinned in `tests.yml` (`SUPABASE_CLI_VERSION`). Dependabot
bumps action refs but not action inputs, so that one is a manual bump.

## Sharp edges

- The Python rule set is shared: repo-root `ruff.toml`, extended from each package's `pyproject.toml`. Change it in one place, not three. mypy has no `extend`, so its config is per-package by necessity and the blocks are meant to stay identical.
- `firmware/` and `pipeline/` are consumed by `integration/` as path dependencies. Both need their `[build-system]` table and their `py.typed` marker to stay put, or `integration/` silently degrades every cross-package call to `Any` and `python -m` stops resolving outside the package directory.
- Under jest-expo, `global.fetch` is React Native's XHR polyfill and resolves with `status`/`body` undefined against plain http localhost - live tests must inject a real fetch. `WebSocket` is *not* replaced, so Node's native one is used directly.
- `app/AGENTS.md` pins the Expo SDK doc version to read before writing app code.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
