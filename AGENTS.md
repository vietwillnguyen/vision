# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Test surfaces

Five suites, four runners. `.github/workflows/tests.yml` is the authoritative
list of commands and their working directories - read it before inventing one.

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

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
