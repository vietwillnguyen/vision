# Jest "worker failed to exit gracefully" - evidence

All runs are on base `7f713a9` (main after #94), Node 22.23.1, on a 16-CPU WSL host, against a running local Supabase stack (`supabase status -o env` mapped to `SUPABASE_*` exactly as `.github/workflows/tests.yml` does).
Each run is `CI=true npx jest --ci --maxWorkers=<n>` from `app/`, cycling `--maxWorkers` through 2, 3, 4 and 15.

## Reproduction and result

`loop.sh` runs the loop and counts runs whose output contains "failed to exit gracefully".
Reproduce with `SB_ENV=<file holding supabase status -o env> APP_DIR=$PWD ../docs/superpowers/evidence/jest-worker-exit/loop.sh <label> 10 1 2 3 4 15` from `app/` (third argument `0` runs without the live env).

| Loop | Live Supabase env | Runs | Runs with the warning |
| --- | --- | --- | --- |
| `before-live-loop.txt` | yes | 40 | 40 |
| `before-nolive-loop.txt` | no | 20 | 0 |
| `after-live-loop.txt` | yes | 40 | 0 |
| `after-nolive-loop.txt` | no | 20 | 0 |

`before-live-sample-run.log` and `after-live-sample-run.log` are one full run of each.
Every run in every loop passed all its tests; the warning never failed a run, which is why it went unnoticed as a flake.

## Root cause

Only `__tests__/hooks/useDeviceStatus.live.test.tsx` leaks, and only when the live env is set (it skips otherwise).
Wrapping the test globals' timers and dumping the ones still pending and ref'd after the file's last hook showed exactly one:

```
setTimeout(..., 10000) created
    at node_modules/@supabase/realtime-js/src/phoenix/socketAdapter.ts:97:7
    at SocketAdapter.disconnect (.../socketAdapter.ts:96:12)
    at RealtimeClient.disconnect (.../RealtimeClient.ts:354:37)
```

`RealtimeClient.disconnect()` in `@supabase/realtime-js` 2.119.0 (the latest release) races the socket teardown against `setTimeout(() => resolve('timeout'), 10000)` and never clears that timer when the teardown wins.
The test's cleanup calls it, so the worker that ran the file keeps a ref'd timer for 10s.
If that worker has no other file left to run before the timer fires, jest-worker force-kills it after its 500ms grace and prints the warning.
Locally the whole suite takes 6-20s, so the live file almost always lands in the last 10s of its worker (40 of 40).
On a CI runner the suite is slower and the live file sometimes finishes early enough, hence "sometimes".
`--detectOpenHandles` missed it because it forces an in-band run in which the live file is rarely last.

## Fix

The live test now closes its realtime socket through `disconnectRealtime()`, which captures the timers the disconnect starts and clears them once the socket reports a clean close, and stops both clients' auth auto-refresh tickers.

## Regression guard

`app/jest.timerLeakEnv.js` is the suite's test environment: the jest-expo preset's environment plus a per-file check at teardown that fails any file leaving a ref'd interval, or a ref'd timeout due more than 250ms later, with each timer's creation stack.
`guard-before-test-fix.log` is a full live run with the guard in place and the test fix not yet applied: the live file fails and names the realtime-js timer; every other file passes.
`app/__tests__/jest/timerLeakEnv.test.ts` exercises the guard itself; with the check disabled, two of its three tests fail.
