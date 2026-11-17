#!/usr/bin/env bash
# Run the app jest suite N times on a cold transform cache while every CPU is
# saturated by busy-loop hogs, the way CI (empty cache) runs on a shared,
# contended runner. Prints one PASS/FAIL line per run plus the slowest tests.
#
# usage: run-loaded.sh <runs> <log-dir>   (run from app/)
# JEST_ARGS adds jest flags, e.g. JEST_ARGS='--testTimeout=5000' or
# JEST_ARGS='-c /path/to/other.config.js'.
set -uo pipefail
RUNS=${1:-10}
LOG_DIR=${2:-./loaded-runs}
HOGS=${HOGS:-$(nproc)}
mkdir -p "$LOG_DIR"

hog_pids=()
for _ in $(seq "$HOGS"); do
  ( while :; do :; done ) &
  hog_pids+=($!)
done
trap 'kill "${hog_pids[@]}" 2>/dev/null' EXIT
echo "host: $(nproc) cpus, $HOGS busy-loop hogs, jest maxWorkers default (cpus-1), extra jest args: ${JEST_ARGS:-none}"

pass=0
for i in $(seq "$RUNS"); do
  cache=$(mktemp -d)
  log="$LOG_DIR/run-$(printf %02d "$i").log"
  # The live-Supabase suite is excluded: it needs a running stack and is not
  # one of the container suites under investigation.
  # shellcheck disable=SC2086
  npx jest --ci --verbose --cacheDirectory "$cache" --testPathIgnorePatterns live ${JEST_ARGS:-} >"$log" 2>&1
  rc=$?
  rm -rf "$cache"
  summary=$(grep -E '^Tests:' "$log" | tail -1)
  slowest=$(grep -oE '\(([0-9]+) ms\)' "$log" | tr -d '()ms ' | sort -n | tail -1)
  if [ $rc -eq 0 ]; then pass=$((pass + 1)); verdict=PASS; else verdict=FAIL; fi
  echo "run $i: $verdict | $summary | slowest test ${slowest:-?} ms"
  if [ $rc -ne 0 ]; then
    grep -E '✕|Exceeded timeout|Unable to find' "$log" | head -8 | sed 's/^/    /'
  fi
done
echo "result: $pass/$RUNS runs passed"
