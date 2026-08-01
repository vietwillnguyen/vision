#!/usr/bin/env bash
# End-to-end tests for scripts/hooks/pre-commit.
#
# Each case builds a throwaway git repo in a temp dir, points its
# core.hooksPath at this repo's real hooks directory, and drives real
# `git commit` invocations - the same path a developer hits - rather than
# calling the hook script directly. Dependencies: git, sha256sum, coreutils.
#
# Run: scripts/hooks/tests/test-pre-commit.sh
set -uo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "$0")/../../.." && pwd)
HOOKS_DIR="$REPO_ROOT/scripts/hooks"

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

SPEC_A="docs/superpowers/specs/2026-07-04-visio-pendant-design.md"
SPEC_B="docs/superpowers/specs/2026-07-20-wifi-reonboard-qr-screen-design.md"
SPEC_C="docs/superpowers/specs/2026-07-21-visio-device-provisioning-design.md"

manifest_line() {
  # Mirrors `sha256sum <file>` output, wrapped in the HTML comment the doc carries.
  printf '<!-- source-sha256: %s -->\n' "$(sha256sum "$1")"
}

write_doc() {
  # write_doc [spec...] - an ARCHITECTURE.html whose manifest lists exactly
  # the specs named, hashed at their current working-tree content.
  {
    printf '<!doctype html>\n<body>\n'
    for spec in "$@"; do
      manifest_line "$spec"
    done
    printf '<p>rendered architecture</p>\n</body>\n'
  } >docs/ARCHITECTURE.html
}

# new_repo - fresh repo containing three specs, a doc tracking all three, and
# an unrelated file. Leaves $PWD inside the new repo.
new_repo() {
  cleanup
  workdir=$(mktemp -d)
  cd -- "$workdir" || exit 1
  git init --quiet --initial-branch=main .
  git config user.email test@example.com
  git config user.name "Hook Test"
  git config commit.gpgsign false
  git config core.hooksPath "$HOOKS_DIR"

  mkdir -p docs/superpowers/specs
  printf 'spec A body\n' >"$SPEC_A"
  printf 'spec B body\n' >"$SPEC_B"
  printf 'spec C body\n' >"$SPEC_C"
  printf 'readme\n' >README.md
  write_doc "$SPEC_A" "$SPEC_B" "$SPEC_C"

  git add -A
  git commit --quiet --no-verify -m "fixture"
}

# commit_staged <message> - stage nothing, commit what's staged; captures
# stderr into $hook_stderr and exit status into $hook_status.
commit_staged() {
  hook_stderr=$(git commit -m "$1" 2>&1 >/dev/null)
  hook_status=$?
}

# --- assertions --------------------------------------------------------------

assert_status() {
  [ "$hook_status" = "$1" ] && return 0
  fail "expected exit status $1, got $hook_status. Output:
$hook_stderr"
}

assert_contains() {
  case "$hook_stderr" in
    *"$1"*) return 0 ;;
  esac
  fail "expected output to mention '$1'. Output:
$hook_stderr"
}

assert_not_contains() {
  case "$hook_stderr" in
    *"$1"*)
      fail "expected output NOT to mention '$1'. Output:
$hook_stderr"
      ;;
  esac
}

it() {
  current_case="$1"
  total=$((total + 1))
}

# --- cases -------------------------------------------------------------------

it "commits cleanly when a spec changes and the doc's manifest is updated with it"
new_repo
printf 'spec A changed\n' >"$SPEC_A"
write_doc "$SPEC_A" "$SPEC_B" "$SPEC_C"
git add -A
commit_staged "sync"
assert_status 0

it "blocks a drifted change to the first tracked spec"
new_repo
printf 'spec A changed\n' >"$SPEC_A"
git add "$SPEC_A"
commit_staged "drift A"
assert_status 1
assert_contains "$SPEC_A"
assert_not_contains "$SPEC_B"

it "blocks a drifted change to the second tracked spec"
new_repo
printf 'spec B changed\n' >"$SPEC_B"
git add "$SPEC_B"
commit_staged "drift B"
assert_status 1
assert_contains "$SPEC_B"
assert_not_contains "$SPEC_A"

it "blocks a drifted change to the third tracked spec"
new_repo
printf 'spec C changed\n' >"$SPEC_C"
git add "$SPEC_C"
commit_staged "drift C"
assert_status 1
assert_contains "$SPEC_C"
assert_not_contains "$SPEC_A"

it "names every drifted spec when more than one drifts at once"
new_repo
printf 'spec A changed\n' >"$SPEC_A"
printf 'spec C changed\n' >"$SPEC_C"
git add "$SPEC_A" "$SPEC_C"
commit_staged "drift A and C"
assert_status 1
assert_contains "$SPEC_A"
assert_contains "$SPEC_C"

it "keeps the regeneration command and the --no-verify escape hatch in the error"
new_repo
printf 'spec B changed\n' >"$SPEC_B"
git add "$SPEC_B"
commit_staged "drift B"
assert_status 1
assert_contains "docs/architecture-regeneration.md"
assert_contains "git add docs/ARCHITECTURE.html"
assert_contains "git commit --no-verify"

it "lets --no-verify through despite drift"
new_repo
printf 'spec A changed\n' >"$SPEC_A"
git add "$SPEC_A"
hook_stderr=$(git commit --no-verify -m "bypass" 2>&1 >/dev/null)
hook_status=$?
assert_status 0

it "exits 0 when the doc carries no manifest comments"
new_repo
printf '<!doctype html>\n<body>no manifest</body>\n' >docs/ARCHITECTURE.html
printf 'spec A changed\n' >"$SPEC_A"
git add -A
commit_staged "no manifest"
assert_status 0

it "exits 0 when the doc is absent from the index"
new_repo
git rm --quiet docs/ARCHITECTURE.html
printf 'spec A changed\n' >"$SPEC_A"
git add -A
commit_staged "no doc"
assert_status 0

it "exits 0 for a commit touching neither a tracked spec nor the doc"
new_repo
printf 'readme changed\n' >README.md
git add README.md
commit_staged "unrelated"
assert_status 0

it "exits 0 for a commit touching only the doc"
new_repo
write_doc "$SPEC_A" "$SPEC_B"
git add docs/ARCHITECTURE.html
commit_staged "doc only"
assert_status 0

it "blocks a new spec that the doc's manifest does not track"
new_repo
printf 'brand new spec\n' >docs/superpowers/specs/2026-08-01-new-thing.md
git add -A
commit_staged "untracked spec"
assert_status 1
assert_contains "2026-08-01-new-thing.md"

it "blocks deleting a spec the manifest still tracks"
new_repo
git rm --quiet "$SPEC_B"
commit_staged "delete tracked spec"
assert_status 1
assert_contains "$SPEC_B"

it "ignores a non-spec markdown file elsewhere in docs/"
new_repo
printf 'a plan\n' >docs/superpowers/plans-note.md
git add -A
commit_staged "unrelated doc"
assert_status 0

# --- summary -----------------------------------------------------------------

cd -- "$REPO_ROOT" || exit 1
if [ "$failed" -gt 0 ]; then
  printf '\n%d assertion(s) failed across %d pre-commit hook cases\n' "$failed" "$total" >&2
  exit 1
fi
printf '%d/%d pre-commit hook cases passed\n' "$total" "$total"
