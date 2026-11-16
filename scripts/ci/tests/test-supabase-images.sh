#!/usr/bin/env bash
# Tests for scripts/ci/supabase-images.sh.
#
# A stub `docker` on PATH records every call to a log and answers from a tiny
# on-disk image store, so the cases exercise the real script end to end
# without a daemon or a network. Dependencies: bash, coreutils.
#
# Run: scripts/ci/tests/test-supabase-images.sh
set -uo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "$0")/../../.." && pwd)
SCRIPT="$REPO_ROOT/scripts/ci/supabase-images.sh"

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

# Stub docker. State lives in $STUB_DIR:
#   images/<escaped ref>   present images (escaped: / and : become _)
#   pull-fail/<escaped>    number of remaining pulls of that ref that fail
#   containers             lines of "<project> <image>" for `docker ps`
#   calls                  every invocation, one per line
write_stub() {
  mkdir -p "$workdir/bin" "$STUB_DIR/images" "$STUB_DIR/pull-fail"
  : > "$STUB_DIR/calls"
  : > "$STUB_DIR/containers"
  cat > "$workdir/bin/docker" <<'STUB'
#!/usr/bin/env bash
esc() { printf '%s' "$1" | tr '/:' '__'; }
printf '%s\n' "$*" >> "$STUB_DIR/calls"
case "$1 $2" in
  "image inspect")
    [ -e "$STUB_DIR/images/$(esc "$3")" ] && exit 0
    echo "Error: No such image: $3" >&2; exit 1 ;;
  "pull "*)
    f="$STUB_DIR/pull-fail/$(esc "$2")"
    if [ -e "$f" ]; then
      n=$(cat "$f")
      if [ "$n" -gt 0 ]; then
        echo $((n - 1)) > "$f"
        echo "Error response from daemon: toomanyrequests: Rate exceeded" >&2; exit 1
      fi
    fi
    touch "$STUB_DIR/images/$(esc "$2")"; exit 0 ;;
  "tag "*)
    [ -e "$STUB_DIR/images/$(esc "$2")" ] || exit 1
    touch "$STUB_DIR/images/$(esc "$3")"; exit 0 ;;
  "rmi "*)
    rm -f "$STUB_DIR/images/$(esc "$2")"; exit 0 ;;
  "save -o"|"load -i")
    exit 0 ;;
  "ps --filter")
    # docker ps --filter label=com.supabase.cli.project=<p> --format {{.Image}}
    p="${3#label=com.supabase.cli.project=}"
    awk -v p="$p" '$1 == p { print $2 }' "$STUB_DIR/containers"; exit 0 ;;
esac
echo "stub docker: unhandled: $*" >&2
exit 2
STUB
  chmod +x "$workdir/bin/docker"
}

new_case() {
  current_case="$1"
  total=$((total + 1))
  cleanup
  workdir=$(mktemp -d)
  export STUB_DIR="$workdir/stub"
  write_stub
  MANIFEST="$workdir/images.txt"
  cat > "$MANIFEST" <<'EOF'
# comment line

public.ecr.aws/supabase/postgres:17.6.1.140
public.ecr.aws/supabase/gotrue:v2.192.0   # trailing comment
EOF
}

present() { touch "$STUB_DIR/images/$(printf '%s' "$1" | tr '/:' '__')"; }
is_present() { [ -e "$STUB_DIR/images/$(printf '%s' "$1" | tr '/:' '__')" ]; }

run() {
  PATH="$workdir/bin:$PATH" \
    SUPABASE_IMAGES_MANIFEST="$MANIFEST" \
    SUPABASE_IMAGES_RETRY_DELAYS="${RETRY_DELAYS:-0 0}" \
    "$SCRIPT" "$@" > "$workdir/out" 2> "$workdir/err"
}

# --- cases -------------------------------------------------------------------

new_case "list prints manifest images without comments or blanks"
if run list; then
  expected=$'public.ecr.aws/supabase/postgres:17.6.1.140\npublic.ecr.aws/supabase/gotrue:v2.192.0'
  [ "$(cat "$workdir/out")" = "$expected" ] || fail "got: $(cat "$workdir/out")"
else
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "seed pulls each missing image from the GHCR mirror and tags it with the ECR name"
if run seed; then
  for ref in public.ecr.aws/supabase/postgres:17.6.1.140 public.ecr.aws/supabase/gotrue:v2.192.0; do
    is_present "$ref" || fail "$ref not present after seed"
  done
  grep -qx 'pull ghcr.io/supabase/postgres:17.6.1.140' "$STUB_DIR/calls" || fail "did not pull the ghcr mirror"
  grep -q '^pull public.ecr.aws' "$STUB_DIR/calls" && fail "pulled from public.ecr.aws"
  is_present ghcr.io/supabase/postgres:17.6.1.140 && fail "left the ghcr.io tag behind"
else
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "seed skips images that are already present, touching no registry"
present public.ecr.aws/supabase/postgres:17.6.1.140
present public.ecr.aws/supabase/gotrue:v2.192.0
if run seed; then
  grep -q '^pull' "$STUB_DIR/calls" && fail "pulled although every image was present: $(grep '^pull' "$STUB_DIR/calls")"
else
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "seed retries a rate-limited mirror pull and then succeeds"
echo 2 > "$STUB_DIR/pull-fail/ghcr.io_supabase_postgres_17.6.1.140"
if run seed; then
  n=$(grep -cx 'pull ghcr.io/supabase/postgres:17.6.1.140' "$STUB_DIR/calls")
  [ "$n" = 3 ] || fail "expected 3 pull attempts, got $n"
  is_present public.ecr.aws/supabase/postgres:17.6.1.140 || fail "image not present after retries"
else
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "seed gives up after a bounded number of attempts"
echo 99 > "$STUB_DIR/pull-fail/ghcr.io_supabase_postgres_17.6.1.140"
if run seed; then
  fail "seed succeeded although every pull failed"
else
  n=$(grep -cx 'pull ghcr.io/supabase/postgres:17.6.1.140' "$STUB_DIR/calls")
  [ "$n" = 3 ] || fail "expected 3 pull attempts (1 + 2 retries), got $n"
  grep -q 'postgres:17.6.1.140' "$workdir/err" || fail "error does not name the image: $(cat "$workdir/err")"
fi

new_case "seed rejects a manifest entry that is not a public.ecr.aws/supabase image"
echo "docker.io/library/redis:7" >> "$MANIFEST"
if run seed; then
  fail "accepted a non-ECR manifest entry"
else
  grep -q 'redis' "$workdir/err" || fail "error does not name the entry: $(cat "$workdir/err")"
  grep -q '^pull' "$STUB_DIR/calls" && fail "pulled before validating the manifest"
fi

new_case "save writes every manifest image into one archive"
present public.ecr.aws/supabase/postgres:17.6.1.140
present public.ecr.aws/supabase/gotrue:v2.192.0
if run save "$workdir/images.tar"; then
  grep -qx 'save -o '"$workdir"'/images.tar public.ecr.aws/supabase/postgres:17.6.1.140 public.ecr.aws/supabase/gotrue:v2.192.0' "$STUB_DIR/calls" \
    || fail "unexpected save call: $(grep '^save' "$STUB_DIR/calls")"
else
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "verify passes when every running container image is in the manifest"
printf '%s\n' "vision public.ecr.aws/supabase/postgres:17.6.1.140" "vision public.ecr.aws/supabase/gotrue:v2.192.0" \
  "other public.ecr.aws/supabase/edge-runtime:v1.74.2" > "$STUB_DIR/containers"
if ! run verify vision; then
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "verify fails and names an image the manifest does not list"
printf '%s\n' "vision public.ecr.aws/supabase/postgres:17.6.1.140" "vision ghcr.io/supabase/realtime:v2.112.1" > "$STUB_DIR/containers"
if run verify vision; then
  fail "verify passed with an unlisted image"
else
  grep -q 'ghcr.io/supabase/realtime:v2.112.1' "$workdir/err" || fail "error does not name the image: $(cat "$workdir/err")"
fi

new_case "verify fails when no container of the project is running"
if run verify vision; then
  fail "verify passed with no containers"
fi

new_case "verify defaults to the project_id in supabase/config.toml"
printf '%s\n' "vision public.ecr.aws/supabase/postgres:17.6.1.140" > "$STUB_DIR/containers"
if run verify; then
  grep -qx 'ps --filter label=com.supabase.cli.project=vision --format {{.Image}}' "$STUB_DIR/calls" \
    || fail "did not filter on project vision: $(grep '^ps' "$STUB_DIR/calls")"
else
  fail "exit $? ($(cat "$workdir/err"))"
fi

new_case "unknown command exits non-zero with usage"
if run frobnicate; then
  fail "accepted an unknown command"
else
  grep -qi 'usage' "$workdir/err" || fail "no usage text: $(cat "$workdir/err")"
fi

# --- summary -----------------------------------------------------------------

printf '%d/%d passed\n' "$((total - failed))" "$total"
[ "$failed" = 0 ]
