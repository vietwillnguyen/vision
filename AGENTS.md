# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Python projects: `[build-system]` is not optional

The repo has three independent uv projects (`firmware/`, `pipeline/`, `integration/`), so check each one separately.
A `pyproject.toml` with no `[build-system]` table makes uv classify the project as **virtual**: it installs the dependencies and never installs the project's own package.
`uv.lock` records this as `source = { virtual = "." }` (versus `editable = "."`), which is the fastest way to check.
The package then imports only via the implicit current-directory entry `python -m` puts on `sys.path[0]`, so anything invoked from another directory - a console script, a systemd unit without a matching `WorkingDirectory=`, an operator over SSH - fails with `ModuleNotFoundError`.
This shipped to the bring-up device in `firmware/`; see `firmware/tests/test_preflight_entrypoint.py` for the regression test.
`pipeline/` still has it (its documented `python -m pipeline` entrypoint works only from `pipeline/`).

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
