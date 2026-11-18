#!/usr/bin/env bash
# Relock integration/uv.lock after a Dependabot bump to firmware/ or pipeline/.
#
# integration/pyproject.toml declares ../firmware and ../pipeline as
# [tool.uv.sources] path dependencies, and integration/uv.lock records their
# `requires-dist` metadata verbatim. Editing a specifier in either sibling's
# pyproject.toml therefore invalidates integration/uv.lock, and the integration
# jobs' `uv run --locked` refuses to run: "The lockfile at uv.lock needs to be
# updated, but --locked was provided." Dependabot only relocks the directory it
# bumps, so its firmware/ and pipeline/ pull requests arrive with a stale
# integration lock and a red integration job - #79 and #81 both failed that way
# and were relocked by hand in #90.
#
# This is the relock half of .github/workflows/dependabot-relock.yml. It is a
# script rather than inline `run:` steps so its decision logic can be exercised
# offline by scripts/ci/tests/test-relock-integration.sh, with no network, no
# real remote and no Dependabot pull request.
#
# It operates on the git working tree it is *run from*, not on the checkout it
# lives in: the workflow runs the trusted copy from the base branch against a
# second, sparse checkout of the pull request's head. Every path below is
# resolved from `git rev-parse --show-toplevel` of the current directory, which
# is why this script does not locate the repo from $BASH_SOURCE the way
# supabase-images.sh does.
#
# Locking integration/ needs only firmware/pyproject.toml,
# pipeline/pyproject.toml, integration/pyproject.toml and integration/uv.lock on
# disk - no package source - and both uv calls below pass --no-build, so they
# resolve without building a single source distribution and no build backend
# from the pull request's dependency tree ever runs here. A bump that genuinely needed a build fails
# loudly instead of executing it, and is relocked by hand as #90 was.
#
#   usage: scripts/ci/relock-integration.sh <branch>
#
# Exits 0 after pushing the relock commit, and also 0 when the lock was already
# current - the common case, since most bumps touch a dependency integration/
# does not share.
#
# Env: RELOCK_DRY_RUN=1 to stop before pushing, RELOCK_DISPATCH (workflow files
#      to dispatch after the push, default the three gating workflows; empty to
#      dispatch none), RELOCK_REPO (default $GITHUB_REPOSITORY).
set -euo pipefail

log() { printf 'relock-integration: %s\n' "$*" >&2; }

die() {
  log "$*"
  exit 1
}

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  echo "usage: $0 <branch>" >&2
  exit 2
fi
branch="$1"

repo="${RELOCK_REPO:-${GITHUB_REPOSITORY:-}}"
read -r -a dispatch <<< "${RELOCK_DISPATCH-tests.yml lint.yml hooks.yml}"
if [ "${#dispatch[@]}" -gt 0 ] && [ -z "$repo" ]; then
  die "cannot dispatch workflows: neither RELOCK_REPO nor GITHUB_REPOSITORY is set"
fi

root="$(git rev-parse --show-toplevel)"
lock="integration/uv.lock"

# A missing manifest would make `uv lock --check` fail for the wrong reason and
# read as a stale lock, so check the sparse checkout is complete first rather
# than relocking against a tree that is missing a path dependency.
for path in integration/pyproject.toml "$lock" firmware/pyproject.toml pipeline/pyproject.toml; do
  [ -f "$root/$path" ] || die "$path is missing; the checkout does not include every manifest uv lock reads"
done

# The whole point of the job: decide whether anything needs doing at all. A
# bump to a dependency integration/ does not share leaves its lock current, and
# this is the branch that keeps the workflow a no-op on those pull requests.
if ( cd "$root/integration" && uv lock --check --no-build ); then
  log "$lock is already current; nothing to do"
  exit 0
fi

log "$lock is stale; relocking"
# --no-build is load-bearing, not an optimization. This job holds a token that
# can write to the branch, and --no-build means uv resolves from declared
# metadata and published wheels only, never by executing a dependency's build
# backend.
( cd "$root/integration" && uv lock --no-build )

# uv is expected to rewrite exactly one file. Anything else - a uv release that
# starts normalizing pyproject.toml, a stray temporary file - is a surprise in a
# job that can push, so refuse rather than commit it.
changed="$(git -C "$root" status --porcelain)"
[ -n "$changed" ] || die "uv lock rewrote nothing although --check reported $lock stale"
# Porcelain v1 format is two status columns, a space, then the path, so
# substr($0, 4) is the path even when git quotes it.
unexpected="$(printf '%s\n' "$changed" | awk -v lock="$lock" 'substr($0, 4) != lock')"
[ -z "$unexpected" ] || die "refusing to commit; uv lock touched more than $lock:
$unexpected"

git -C "$root" -c "user.name=github-actions[bot]" \
  -c "user.email=41898282+github-actions[bot]@users.noreply.github.com" \
  commit --quiet -m "chore(deps): relock integration/uv.lock" \
  -m "integration/ path-depends on ../firmware and ../pipeline, so a bump to
either one's pyproject.toml leaves integration/uv.lock stale and the
integration jobs' \`uv run --locked\` refuses to run. Regenerated by
.github/workflows/dependabot-relock.yml." -- "$lock"

if [ "${RELOCK_DRY_RUN:-}" = 1 ]; then
  log "RELOCK_DRY_RUN=1; committed but not pushing to origin $branch"
  exit 0
fi

# No --force and no lease: if Dependabot rebased the branch while this job ran,
# the push is rejected and the next run on the new head relocks it again.
log "pushing to origin $branch"
git -C "$root" push origin "HEAD:refs/heads/$branch"

# The push alone leaves the relocked head with no checks on it. A push made with
# GITHUB_TOKEN creates no workflow run - "events triggered by the GITHUB_TOKEN
# will not create a new workflow run" - and `workflow_dispatch` is one of the two
# documented exceptions that always do:
# https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow
#
# A dispatched run's jobs become check runs on the commit its ref resolves to,
# under the same names they carry on a pull_request run, which is what the pull
# request's checks and any required status check read. So dispatching each gating
# workflow on the branch is what actually turns the pull request green.
#
# A dispatch that fails does not fail this script: the relock is already pushed,
# and that is the part that used to need a hand-made pull request. The warning
# below is an Actions annotation so the run surfaces it rather than burying it.
# The known case is a Dependabot branch cut before this workflow landed, whose
# copy of the target file has no workflow_dispatch trigger yet; Dependabot
# recreating or rebasing the branch clears it.
for workflow in ${dispatch[@]+"${dispatch[@]}"}; do
  log "dispatching $workflow on $branch"
  if ! out="$(gh workflow run "$workflow" --repo "$repo" --ref "$branch" 2>&1)"; then
    printf '::warning title=Relock pushed but %s was not dispatched::%s
'       "$workflow" "$out"
    log "could not dispatch $workflow: $out"
  fi
done
