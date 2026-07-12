# Regenerating docs/ARCHITECTURE.html

`docs/ARCHITECTURE.html` is a rendered view of the source-of-truth design spec at
`docs/superpowers/specs/2026-07-04-visio-pendant-design.md`.
Edit the spec, not the HTML directly - then regenerate the HTML from it.

## Why this isn't fully automatic

Regenerating the page requires judgment (what changed, how to phrase it, which
diagram needs a new node) - it is not a deterministic markdown-to-HTML
conversion, so it is not something a git hook can safely run unattended on
every commit.
Instead:

- A **pre-commit hook** (`scripts/hooks/pre-commit`, wired via `git config
  core.hooksPath scripts/hooks` - already set in this repo) blocks any commit
  that changes the spec file without also updating `docs/ARCHITECTURE.html`'s
  embedded `source-sha256` comment.
  It is a fast, deterministic staleness check, not a generator.
- The **regeneration step** below is the thing that actually updates the page.
  Run it yourself, or ask Claude Code to run it, whenever you've edited the
  spec.

## Regeneration step

After editing `docs/superpowers/specs/2026-07-04-visio-pendant-design.md`, run:

```sh
claude -p "Regenerate docs/ARCHITECTURE.html from docs/superpowers/specs/2026-07-04-visio-pendant-design.md. Update every section that changed, keep the existing DaisyUI/Tailwind/Mermaid structure and theme, then update the <!-- source-sha256: ... --> comment near the top of the file to sha256sum of the current spec file."
```

Or, in an interactive Claude Code session, just ask: "regenerate the architecture doc."

If the change is large or you want to review the rendering before committing,
open it in Lavish Editor instead of running headless:

```sh
npx -y lavish-axi docs/ARCHITECTURE.html
npx -y lavish-axi poll docs/ARCHITECTURE.html
```

Either way, finish with:

```sh
git add docs/ARCHITECTURE.html
```

so the pre-commit hook sees the updated hash in the same commit as the spec change.

## Bootstrapping a new clone

`core.hooksPath` is local git config, not something git tracks in the repo
itself. Run this once per clone:

```sh
git config core.hooksPath scripts/hooks
```

(Worktrees of this repo share the same `.git`, so this only needs to be run once per clone, not once per worktree.)

## Bypassing the check

`git commit --no-verify` skips the hook for a single commit - use it only when
you have a deliberate reason the doc is being left stale (e.g. a WIP branch
you'll squash later).
