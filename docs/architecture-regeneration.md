# Regenerating docs/ARCHITECTURE.html

`docs/ARCHITECTURE.html` is a rendered view of every source-of-truth design spec under
`docs/superpowers/specs/`:

- `2026-07-04-visio-pendant-design.md` - the pendant design spec.
- `2026-07-20-wifi-reonboard-qr-screen-design.md` - the WiFi + auth re-onboarding QR screen.
- `2026-07-21-visio-device-provisioning-design.md` - device provisioning and runtime preflight.

Edit the specs, not the HTML directly - then regenerate the HTML from them.

## The source-sha256 manifest

Near the top of `docs/ARCHITECTURE.html`, one comment line per spec records the spec's
path and the sha256 of the content the page was rendered from:

```html
<!-- source-sha256: <sha256>  docs/superpowers/specs/<spec>.md -->
```

Each payload is verbatim `sha256sum` output, so the whole manifest checks in one pipe:

```sh
sed -n 's|^<!-- source-sha256: \(.*\) -->$|\1|p' docs/ARCHITECTURE.html | sha256sum -c -
```

Regenerate the manifest with the inverse of that command:

```sh
for f in docs/superpowers/specs/*.md; do printf '<!-- source-sha256: %s -->\n' "$(sha256sum "$f")"; done
```

A spec with no manifest line is a spec nothing guards, so the hook rejects one being
added to `docs/superpowers/specs/` without a matching line - the earlier single-spec
version of this check silently covered one spec out of three.

Retiring a spec is the same contract in reverse: delete the spec and drop its manifest
line in the same commit.
Deleting the spec while the manifest still lists it is reported as drift, because the
page would keep claiming to render a file that no longer exists.

## Why this isn't fully automatic

Regenerating the page requires judgment (what changed, how to phrase it, which
diagram needs a new node) - it is not a deterministic markdown-to-HTML
conversion, so it is not something a git hook can safely run unattended on
every commit.
Instead:

- A **pre-commit hook** (`scripts/hooks/pre-commit`, wired via `git config
  core.hooksPath scripts/hooks`, which every clone must set - see
  [Bootstrapping a new clone](#bootstrapping-a-new-clone)) blocks any commit
  that changes a spec file without also updating that spec's line in
  `docs/ARCHITECTURE.html`'s embedded `source-sha256` manifest.
  It is a fast, deterministic staleness check, not a generator.
  Its own tests live in `scripts/hooks/tests/test-pre-commit.sh` and run in CI
  via [`.github/workflows/hooks.yml`](../.github/workflows/hooks.yml).
- The **regeneration step** below is the thing that actually updates the page.
  Run it yourself, or ask Claude Code to run it, whenever you've edited a
  spec.

## Regeneration step

After editing any spec under `docs/superpowers/specs/`, run:

```sh
claude -p "Regenerate docs/ARCHITECTURE.html from the specs in docs/superpowers/specs/. Update every section that changed, keep the existing DaisyUI/Tailwind/Mermaid structure and theme, then rewrite the source-sha256 manifest comments near the top of the file so there is one line per spec and each line matches that spec's sha256sum output."
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

## Testing the hook

The hook has its own end-to-end suite - it gates every commit, so it is worth
proving rather than trusting:

```sh
scripts/hooks/tests/test-pre-commit.sh
```

Each case builds a throwaway git repo in a temp dir, points its `core.hooksPath`
at this repo's real `scripts/hooks`, and drives real `git commit` invocations.
It needs only `git` and `sha256sum` - no test framework, no package manager.

## Bypassing the check

`git commit --no-verify` skips the hook for a single commit - use it only when
you have a deliberate reason the doc is being left stale (e.g. a WIP branch
you'll squash later).
