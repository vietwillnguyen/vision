#!/usr/bin/env bash
# usage: loop.sh <label> <runs> <live:1|0> <workers...>
set -u
label=$1; runs=$2; live=$3; shift 3
out="${OUT_DIR:-/tmp/jest-loop}"/$label
mkdir -p "$out"; summary=$out/summary.txt; : > "$summary"
cd "${APP_DIR:-$PWD}"
if [ "$live" = 1 ]; then
  set -a; . "$SB_ENV"; set +a
  export SUPABASE_URL=$API_URL SUPABASE_ANON_KEY=$ANON_KEY SUPABASE_SERVICE_ROLE_KEY=$SERVICE_ROLE_KEY
else
  unset SUPABASE_URL SUPABASE_ANON_KEY SUPABASE_SERVICE_ROLE_KEY
fi
i=0
for ((r=1;r<=runs;r++)); do
  for w in "$@"; do
    i=$((i+1))
    log=$out/run-$(printf %03d $i)-w$w.log
    start=$(date +%s)
    CI=true npx jest --ci --maxWorkers=$w > "$log" 2>&1; rc=$?
    dur=$(( $(date +%s)-start ))
    leak=$(grep -c "failed to exit gracefully" "$log")
    tests=$(grep -E "^Tests:" "$log" | tail -1)
    echo "run=$i workers=$w live=$live rc=$rc dur=${dur}s leak=$leak $tests" | tee -a "$summary"
  done
done
echo "TOTAL runs=$i leaks=$(grep -c 'leak=[1-9]' "$summary")" | tee -a "$summary"
