# Jest container-suite timeouts under load - evidence

All runs are on base `b809807` (main after #71, #72, #75, #76, #83, #70), on a 16-CPU WSL host.

## Reproduction

`run-loaded.sh` runs the full app jest suite (live-Supabase suite excluded) on a fresh, empty transform cache each time, like CI, while one busy-loop process per CPU saturates the host.

- `before-fix-5-loaded-runs.txt`: origin/main's `app/jest.config.js` (15s `testTimeout`, 10s RNTL `asyncUtilTimeout`).
  0 of 5 runs passed.
  The same 5 tests failed every run, each the first rendering test of its suite, at 19-26s.
- `after-fix-10-loaded-runs.txt`: this branch, timeouts unchanged.
  10 of 10 runs passed, slowest test 1.3-3.6s.
- `after-fix-5-loaded-runs-jest-default-5s-timeout.txt`: this branch with jest's default 5s `testTimeout`, to show the fix does not depend on the raised timeout.
  5 of 5 runs passed, slowest test 1.8-2.8s.

## Root cause

`react-native` exports its components through lazy getters, and so does `ReactNativePrivateInterface`, the renderer's entry point into it.
So `import { Text, Button } from 'react-native'` loads nothing until a render touches `Text`.
On a cold cache that first touch Babel-transforms the renderer (`ReactFabric-dev.js` alone is ~1.5s), Animated, Pressability, and the DOM web-API shims behind `ReactFabricPublicInstance`.
That happens inside the first rendering test of every suite, so it counts against that test's timeout and RNTL's `waitFor` budget.
With every worker transforming the same files at once on a loaded host, it reached 20s+.

`trace/` wraps babel-jest to log every transform and attributes it to the test running in that worker:

- `in-test-transforms-before.txt`: 616 modules and ~170s of Babel CPU landed inside running tests; that unloaded trace run itself had 2 failures.
- `in-test-transforms-after.txt`: 1 module, 21ms.

Reproduce with `APP_DIR=$PWD TRACE_OUT=/tmp/t.txt npx jest -c ../docs/superpowers/evidence/app-jest-container-flake/trace/jest.trace.config.js --cacheDirectory $(mktemp -d)` from `app/`, adding `NO_PRELOAD=1` for the before case, then `python3 .../trace/summ.py /tmp/t.txt`.

## Fix

`app/jest.preload.js` is a jest `setupFiles` entry that touches those lazy exports.
Setup files run before the test clock starts and have no timeout, so the one-time transform no longer counts against any test.
