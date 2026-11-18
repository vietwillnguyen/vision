#!/usr/bin/env bash
# Tests for scripts/ci/relock-integration.sh.
#
# A stub `uv` on PATH records every call and rewrites files on command, and the
# remote is a local bare repository, so the cases exercise the real script end
# to end without a network, a uv install or a Dependabot pull request. The
# fixture also reproduces the workflow's checkout shape - a non-cone sparse
# clone holding only the four manifests `uv lock` reads - so the cases cover the
# thing that shape has to get right: a commit made in it must still carry every
# file outside the sparse set. Dependencies: bash, coreutils, git.
#
# What no offline test can cover is named in .github/workflows/dependabot-relock.yml:
# whether the relock push re-runs tests.yml and lint.yml is a property of
# GITHUB_TOKEN on a real Dependabot pull request.
#
# Run: scripts/ci/tests/test-relock-integration.sh
set -uo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "$0")/../../.." && pwd)
SCRIPT="$REPO_ROOT/scripts/ci/relock-integration.sh"
BRANCH="dependabot/uv/pipeline/httpx-0.28.1"

total=0
failed=0
current_case=""
workdir=""

cleanup() {
  [ -n "$workdir" ] && [ -d "$workdir" ] && rm -rf -- "$workdir"
}
trap cleanup EXIT

fail() {
  printf 'FAIL: %s\n      %s\n' "$current_case" "$1" >&2
  failed=$((failed + 1))
}

# --- fixture -----------------------------------------------------------------

# Stub uv. State lives in $STUB_DIR:
#   check-stale      present => `uv lock --check` fails as it does on a stale lock
#   writes           lines of "<path>|<content>" that `uv lock` writes, relative
#                    to the repo root; absent => it writes the usual relock
#   lock-exit        exit code for `uv lock` (default 0)
#   calls            every invocation, one per line
write_stub() {
  mkdir -p "$workdir/bin"
  : > "$STUB_DIR/calls"
  cat > "$workdir/bin/uv" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_DIR/calls"
root=$(git rev-parse --show-toplevel)
case "$*" in
  "lock --check")
    if [ -e "$STUB_DIR/check-stale" ]; then
      echo "error: The lockfile at \`uv.lock\` needs to be updated, but \`--check\` was provided." >&2
      exit 2
    fi
    exit 0 ;;
  "lock --no-build")
    if [ -e "$STUB_DIR/writes" ]; then
      while IFS='|' read -r path content; do
        [ -n "$path" ] && printf '%s\n' "$content" > "$root/$path"
      done < "$STUB_DIR/writes"
    else
      echo "relocked by stub uv" > "$root/integration/uv.lock"
    fi
    exit "$(cat "$STUB_DIR/lock-exit" 2>/dev/null || echo 0)" ;;
esac
echo "stub uv: unhandled: $*" >&2
exit 2
STUB
  chmod +x "$workdir/bin/uv"
  # Stub gh. Records every `gh workflow run` and fails for any workflow file
  # listed in $STUB_DIR/dispatch-fail, one per line.
  cat > "$workdir/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_DIR/calls"
if [ "$1 $2" = "workflow run" ] \
  && [ -e "$STUB_DIR/dispatch-fail" ] && grep -qxF "$3" "$STUB_DIR/dispatch-fail"; then
  echo "could not create workflow dispatch event: HTTP 422: Workflow does not have 'workflow_dispatch' trigger" >&2
  exit 1
fi
exit 0
STUB
  chmod +x "$workdir/bin/gh"
}

# origin: a bare repository. seed: a full checkout used to seed it and to move
# the branch underneath the script. head: the non-cone sparse checkout the
# workflow's second actions/checkout produces.
write_repo() {
  git init -q --bare "$workdir/origin"
  # Records the push itself into the same ordered log as the uv and gh calls, so
  # a case can assert what happened before what. git runs receive-pack as a
  # child of the pushing process, so the hook inherits $STUB_DIR.
  cat > "$workdir/origin/hooks/post-receive" <<'HOOK'
#!/usr/bin/env bash
while read -r _old _new ref; do printf 'push %s\n' "$ref" >> "$STUB_DIR/calls"; done
HOOK
  chmod +x "$workdir/origin/hooks/post-receive"
  local s="$workdir/seed"
  git init -q -b main "$s"
  git -C "$s" config user.email test@example.invalid
  git -C "$s" config user.name test
  mkdir -p "$s/firmware/visio_recorder" "$s/pipeline" "$s/integration" "$s/app/src"
  echo "source" > "$s/firmware/visio_recorder/recorder.py"
  echo "firmware manifest" > "$s/firmware/pyproject.toml"
  echo "firmware lock" > "$s/firmware/uv.lock"
  echo "pipeline manifest" > "$s/pipeline/pyproject.toml"
  echo "integration manifest" > "$s/integration/pyproject.toml"
  echo "integration lock" > "$s/integration/uv.lock"
  echo "app source" > "$s/app/src/index.ts"
  git -C "$s" add -A
  git -C "$s" commit -qm "init"
  git -C "$s" remote add origin "$workdir/origin"
  git -C "$s" push -q origin main
  # The Dependabot commit: a sibling manifest bumped, its own lock relocked,
  # integration/uv.lock left stale.
  git -C "$s" checkout -q -b "$BRANCH"
  echo "pipeline manifest bumped" > "$s/pipeline/pyproject.toml"
  git -C "$s" commit -qam "chore(deps): bump httpx in /pipeline"
  git -C "$s" push -q origin "$BRANCH"

  git clone -q --no-checkout "$workdir/origin" "$workdir/head"
  git -C "$workdir/head" sparse-checkout init --no-cone
  git -C "$workdir/head" sparse-checkout set --no-cone \
    firmware/pyproject.toml pipeline/pyproject.toml \
    integration/pyproject.toml integration/uv.lock
  git -C "$workdir/head" checkout -q "$BRANCH"
}

new_case() {
  current_case="$1"
  total=$((total + 1))
  cleanup
  workdir=$(mktemp -d)
  export STUB_DIR="$workdir/stub"
  mkdir -p "$STUB_DIR"
  write_stub
  write_repo
  # Seeding the fixture pushed main and the Dependabot commit through the hook
  # above; the cases only care about what the script itself does.
  : > "$STUB_DIR/calls"
  remote_head_before=$(git -C "$workdir/origin" rev-parse "$BRANCH")
}

stale() { touch "$STUB_DIR/check-stale"; }

run() {
  ( cd "$workdir/head" && PATH="$workdir/bin:$PATH" \
      RELOCK_REPO="${RELOCK_REPO-vietwillnguyen/vision}" "$SCRIPT" "$@" ) \
    > "$workdir/out" 2> "$workdir/err"
}

dispatches() { grep '^workflow run ' "$STUB_DIR/calls" || true; }

remote_head() { git -C "$workdir/origin" rev-parse "$BRANCH"; }
remote_moved() { [ "$(remote_head)" != "$remote_head_before" ]; }

# --- cases -------------------------------------------------------------------

new_case "does nothing when integration/uv.lock is already current"
if run "$BRANCH"; then
  grep -qx 'lock --no-build' "$STUB_DIR/calls" && fail "relocked although the lock was current"
  remote_moved && fail "pushed although the lock was current"
  [ -z "$(dispatches)" ] || fail "dispatched although the lock was current: $(dispatches)"
  grep -q 'already current' "$workdir/err" || fail "did not say it was current: $(cat "$workdir/err")"
else
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "relocks a stale lock and pushes the commit to the branch"
stale
if run "$BRANCH"; then
  remote_moved || fail "did not push"
  head=$(remote_head)
  changed=$(git -C "$workdir/origin" diff --name-only "$remote_head_before" "$head")
  [ "$changed" = "integration/uv.lock" ] || fail "commit changed more than the lock: $changed"
  [ "$(git -C "$workdir/origin" show "$head:integration/uv.lock")" = "relocked by stub uv" ] \
    || fail "pushed lock is not the relocked one"
  parent=$(git -C "$workdir/origin" rev-parse "$head^")
  [ "$parent" = "$remote_head_before" ] || fail "commit is not on top of the branch head"
else
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "the commit made in a sparse checkout still carries the unchecked-out files"
stale
if run "$BRANCH"; then
  tree=$(git -C "$workdir/origin" ls-tree -r --name-only "$(remote_head)" | sort | tr '\n' ' ')
  for path in app/src/index.ts firmware/uv.lock firmware/visio_recorder/recorder.py; do
    case " $tree " in *" $path "*) ;; *) fail "$path is missing from the pushed tree: $tree" ;; esac
  done
else
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "locks with --no-build, so no dependency build backend can run"
stale
if run "$BRANCH"; then
  grep -qx 'lock --no-build' "$STUB_DIR/calls" || fail "uv lock was called without --no-build: $(cat "$STUB_DIR/calls")"
  grep -qx 'lock' "$STUB_DIR/calls" && fail "called a bare uv lock"
else
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "commits with the github-actions[bot] identity, not the runner's git config"
stale
if run "$BRANCH"; then
  author=$(git -C "$workdir/origin" log -1 --format='%an <%ae>' "$BRANCH")
  [ "$author" = "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>" ] \
    || fail "unexpected author: $author"
  subject=$(git -C "$workdir/origin" log -1 --format='%s' "$BRANCH")
  [ "$subject" = "chore(deps): relock integration/uv.lock" ] || fail "unexpected subject: $subject"
else
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "refuses to push when uv rewrites anything but integration/uv.lock"
stale
printf '%s\n' "integration/uv.lock|relocked" "pipeline/pyproject.toml|tampered" > "$STUB_DIR/writes"
if run "$BRANCH"; then
  fail "committed a change outside integration/uv.lock"
else
  remote_moved && fail "pushed although it should have refused"
  grep -q 'pipeline/pyproject.toml' "$workdir/err" || fail "error does not name the path: $(cat "$workdir/err")"
fi

new_case "fails when uv rewrites nothing although the check reported the lock stale"
stale
: > "$STUB_DIR/writes"
if run "$BRANCH"; then
  fail "succeeded with nothing to commit"
else
  remote_moved && fail "pushed with nothing to commit"
  grep -q 'rewrote nothing' "$workdir/err" || fail "unexpected error: $(cat "$workdir/err")"
fi

new_case "propagates a uv lock failure without pushing"
stale
echo 1 > "$STUB_DIR/lock-exit"
: > "$STUB_DIR/writes"
if run "$BRANCH"; then
  fail "succeeded although uv lock failed"
else
  remote_moved && fail "pushed although uv lock failed"
fi

new_case "fails rather than force-pushing when the branch moved mid-run"
stale
echo "rebased by dependabot" > "$workdir/seed/pipeline/pyproject.toml"
git -C "$workdir/seed" commit -qam "chore(deps): bump httpx in /pipeline"
git -C "$workdir/seed" push -q origin "$BRANCH"
moved_head=$(remote_head)
if run "$BRANCH"; then
  fail "pushed onto a branch that had moved"
else
  [ "$(remote_head)" = "$moved_head" ] || fail "clobbered the newer commit"
fi

new_case "RELOCK_DRY_RUN=1 commits locally but does not push"
stale
if RELOCK_DRY_RUN=1 run "$BRANCH"; then
  remote_moved && fail "pushed although RELOCK_DRY_RUN=1"
  [ "$(git -C "$workdir/head" log -1 --format='%s')" = "chore(deps): relock integration/uv.lock" ] \
    || fail "did not commit locally"
else
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "fails when the checkout is missing a manifest uv lock reads"
stale
rm -f "$workdir/head/firmware/pyproject.toml"
if run "$BRANCH"; then
  fail "relocked against an incomplete checkout"
else
  remote_moved && fail "pushed from an incomplete checkout"
  grep -q 'firmware/pyproject.toml' "$workdir/err" || fail "error does not name the path: $(cat "$workdir/err")"
  grep -qx 'lock --check' "$STUB_DIR/calls" && fail "ran uv before checking the checkout"
fi

new_case "dispatches every gating workflow on the branch after pushing"
stale
if run "$BRANCH"; then
  expected="workflow run tests.yml --repo vietwillnguyen/vision --ref $BRANCH
workflow run lint.yml --repo vietwillnguyen/vision --ref $BRANCH
workflow run hooks.yml --repo vietwillnguyen/vision --ref $BRANCH"
  [ "$(dispatches)" = "$expected" ] || fail "unexpected dispatches: $(dispatches)"
else
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "dispatches only after the push, never before it"
stale
if run "$BRANCH"; then
  # A dispatch of a branch the relock has not reached yet would run CI on the
  # stale head and report a green that means nothing, so the order is the point.
  first=$(grep -E '^(push |workflow run )' "$STUB_DIR/calls" | head -1)
  [ "$first" = "push refs/heads/$BRANCH" ] \
    || fail "dispatched before the push landed: $(cat "$STUB_DIR/calls")"
else
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "a refused dispatch warns but does not fail the pushed relock"
stale
printf '%s\n' lint.yml > "$STUB_DIR/dispatch-fail"
if run "$BRANCH"; then
  remote_moved || fail "did not push"
  grep -q '^::warning title=Relock pushed but lint.yml was not dispatched::' "$workdir/out" \
    || fail "no warning annotation: $(cat "$workdir/out")"
  grep -q "workflow run hooks.yml" "$STUB_DIR/calls" || fail "stopped dispatching after the failure"
else
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "RELOCK_DISPATCH= dispatches nothing"
stale
if RELOCK_DISPATCH='' run "$BRANCH"; then
  remote_moved || fail "did not push"
  [ -z "$(dispatches)" ] || fail "dispatched with RELOCK_DISPATCH empty: $(dispatches)"
else
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "fails before relocking when the repository for the dispatch is unknown"
stale
if RELOCK_REPO='' GITHUB_REPOSITORY='' run "$BRANCH"; then
  fail "pushed without knowing where to dispatch"
else
  grep -q 'GITHUB_REPOSITORY' "$workdir/err" || fail "unexpected error: $(cat "$workdir/err")"
fi

new_case "RELOCK_DRY_RUN=1 dispatches nothing either"
stale
if RELOCK_DRY_RUN=1 run "$BRANCH"; then
  [ -z "$(dispatches)" ] || fail "dispatched although RELOCK_DRY_RUN=1: $(dispatches)"
else
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "exits with usage when given no branch"
if run; then
  fail "accepted a missing branch argument"
else
  [ "$?" = 2 ] || fail "expected exit 2"
  grep -qi 'usage' "$workdir/err" || fail "no usage text: $(cat "$workdir/err")"
fi

# --- summary -----------------------------------------------------------------

printf '%d/%d passed\n' "$((total - failed))" "$total"
[ "$failed" = 0 ]
