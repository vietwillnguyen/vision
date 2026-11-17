# PR #26 merge resolution: origin/main into worktree-visio-device-provisioning

## Why there were conflicts at all

PR #46 brought main (up to `f5293db`, #44) into this branch, but it was squash-merged, so the branch got main's content without main's commits as ancestors.
`git merge origin/main` therefore picked `451267b` (#25) as the merge base and replayed main's #27/#28/#29/#44 work against a branch that already contained it, reporting 24 conflicting files.
Every one of those 24 was spurious except where main changed the same file again after `f5293db`.

## How it was resolved

The merge is a real two-parent merge commit (`origin/worktree-visio-device-provisioning` + `origin/main` at `ffed638`), with no rebase and no force push.
The tree was resolved as a three-way merge against the true base `f5293db` (`git merge-tree --merge-base=f5293db`), which is the state #46 already merged.
Against that base only two files conflict: `app/jest.config.js` and `app/jest.setup.js`.
Every other file is main's version plus the branch's own delta since `f5293db`, byte for byte.
This was checked mechanically: `git diff origin/main <merge>` and `git diff f5293db <branch tip>` touch the same 29 files with identical hunks, except the two jest files.

After the merge, `git merge-tree origin/main HEAD` is clean, so PR #26 has zero conflicts against main.

## Conflict table

"Branch" and "main" columns say whether that side changed the file after the squash base `f5293db`.

| # | File | Branch changed since f5293db | Main changed since f5293db | Resolution | Rationale |
|---|---|---|---|---|---|
| 1 | `.github/workflows/hooks.yml` | yes (#46) | no | branch | Branch-only change (temporary PR #26 trigger); main has nothing newer. |
| 2 | `.github/workflows/lint.yml` | yes (#45, #46) | no | branch | Branch added the shellcheck job; main has nothing newer. |
| 3 | `AGENTS.md` | yes (#45, #46) | no | branch | Branch-only notes (`[build-system]` section, shellcheck gate); main has nothing newer. |
| 4 | `README.md` | yes (#45, #46, trixie fix) | yes (#47, #58) | auto-merged, both | Hunks do not overlap against the true base; both sides' text kept. |
| 5 | `app/__tests__/App.test.tsx` | no | yes (#58) | main | Typed-query changes from #58; branch had only the pre-squash copy. |
| 6 | `app/__tests__/containers/ArchiveContainer.test.tsx` | no | yes (#58) | main | Same as above. |
| 7 | `app/__tests__/containers/RawFootageContainer.test.tsx` | no | yes (#58) | main | Same as above. |
| 8 | `app/__tests__/containers/ReonboardContainer.test.tsx` | no | yes (#58) | main | Same as above. |
| 9 | `app/__tests__/containers/TodayReelContainer.test.tsx` | no | yes (#58) | main | Same as above. |
| 10 | `app/__tests__/hooks/useSlotThumbnails.test.tsx` | no | yes (#58) | main | Same as above. |
| 11 | `app/jest.config.js` | yes (#46) | yes (#58) | branch | Genuine conflict, see below. |
| 12 | `app/jest.setup.js` | yes (#46, add) | yes (#58, add) | branch | Genuine add/add conflict, see below. |
| 13 | `app/package.json` | no | yes (#58) | main | Adds the `gen:types` script from #58. |
| 14 | `app/src/hooks/useDevice.ts` | no | yes (#58) | main | Typed Supabase queries from #58. |
| 15 | `app/src/hooks/useDeviceStatus.ts` | no | yes (#58) | main | Same as above. |
| 16 | `app/src/hooks/useReel.ts` | no | yes (#58) | main | Same as above. |
| 17 | `app/src/hooks/useSegments.ts` | no | yes (#58) | main | Same as above. |
| 18 | `docs/ARCHITECTURE.html` | yes (provisioning spec) | no | branch | Generated page; branch's copy carries the provisioning spec manifest line and `sha256sum -c` passes on the merge. |
| 19 | `firmware/pyproject.toml` | yes (#45, #46) | no | branch | `[build-system]` and `visio-preflight` console script; main has nothing newer. |
| 20 | `firmware/tests/test_daemon.py` | yes (battery source, #46) | no | branch | Branch-only tests; main has nothing newer. |
| 21 | `firmware/tests/test_flag_button.py` | yes (#46) | no | branch | Branch-only upload-defect tests. |
| 22 | `firmware/tests/test_recording_loop.py` | yes (#46) | no | branch | Branch-only upload-backlog tests. |
| 23 | `firmware/visio_recorder/supabase_clients.py` | yes (#46) | no | branch | Branch-only upload retry fix. |
| 24 | `integration/tests/test_row_schema_contract.py` | no | yes (#58) | main | Rewritten by #58 against the generated schema fixture. |

### The one real conflict: jest timeouts (rows 11 and 12)

Both sides fixed the same cold-cache flake independently: the branch in #46 (`testTimeout: 45000`, `asyncUtilTimeout: 30000`) and main in #58 (`testTimeout: 15000`, `asyncUtilTimeout: 10000`).
Both already wire `setupFilesAfterEnv: ['<rootDir>/jest.setup.js']`, so the only difference is the budget.
The branch's values were kept because they are measured: its comment records `TodayReelContainer > plays today's reel through a signed url` at about 13s on a cold cache, which main's 10s budget cannot cover.
This was confirmed on the merged tree rather than taken on trust:

- Branch budgets, `jest --clearCache` then `jest --ci`: 32/32 suites, 126/126 tests pass ([app-jest-cold-cache.log](app-jest-cold-cache.log)).
- Main's budgets swapped in, same cold cache: that exact test fails at 10349 ms ([app-jest-cold-cache-mains-timeouts.log](app-jest-cold-cache-mains-timeouts.log)).

So taking main's side would have reintroduced the flake on merge; main itself is exposed to it on a cold CI cache until this lands.

## Follow-up commit on top of the merge

`scripts/gen-db-types.sh` arrived from main (#58) and was not in the branch's hand-maintained shellcheck list, whose comment says every shebang file must be listed.
It had one finding (SC2154, `db_url` only assigned through `eval`), fixed by initialising it and failing with a clear message, then added to `lint.yml`.

## Test evidence (all run on the merged tree)

| Surface (tests.yml / lint.yml / hooks.yml) | Command | Result | Log |
|---|---|---|---|
| firmware | `uv run --locked --extra dev pytest` | 289 passed | [firmware-pytest.log](firmware-pytest.log) |
| pipeline | `uv run --locked --extra dev pytest` | 130 passed | [pipeline-pytest.log](pipeline-pytest.log) |
| pgTAP | `supabase test db` (CLI 2.109.0, local stack, all 14 migrations applied) | 9 files, 59 tests, PASS | [pgtap.log](pgtap.log) |
| integration (live Supabase) | `uv run --locked --extra dev pytest` | 7 passed, 0 skipped | [integration-pytest.log](integration-pytest.log) |
| app generated types | `scripts/gen-db-types.sh` + `git diff --exit-code` | no drift | [gen-db-types.log](gen-db-types.log) |
| app | `tsc --noEmit`, `npm run lint`, `npm run typecheck` | all exit 0 | [app-tsc-lint.log](app-tsc-lint.log) |
| app jest (live Supabase, cold cache) | `jest --clearCache && jest --ci --verbose` | 126 passed | [app-jest-cold-cache.log](app-jest-cold-cache.log) |
| app web bundle | `expo export --platform web` | exit 0 | [app-web-export.log](app-web-export.log) |
| python lint | ruff check, ruff format --check, mypy per package | clean | [python-lint.log](python-lint.log) |
| shellcheck | lint.yml file list plus `scripts/gen-db-types.sh` | clean | [gen-db-types.log](gen-db-types.log) |
| hooks | `scripts/hooks/tests/test-pre-commit.sh`, manifest `sha256sum -c`, spec coverage | pass | [hooks.log](hooks.log) |
| setup/preflight scripts | `bash -n setup-device.sh`, `systemd-analyze verify`, `test_setup_device.py` + `test_preflight*.py` | 166 passed; unit only flags the device-only `/opt` venv path | [setup-preflight-scripts.log](setup-preflight-scripts.log) |

### Firmware with hardware simulated

[simulate-preflight.sh](simulate-preflight.sh) runs the real installed `visio-preflight` console script from `/` (proving the entry point resolves outside the repo) in four scenarios, with stub `rpicam-*`, `zbarimg` and `nmcli` binaries on `PATH` and stub `rpi_ws281x`/`gpiozero` modules on `PYTHONPATH`.
Output: [firmware-preflight-simulated-hardware.log](firmware-preflight-simulated-hardware.log).

1. Bare non-Pi host: every missing dependency blocks with its remedy, exit 1.
2. Simulated provisioned Pi: all `[OK]`, exit 0.
3. Simulated Pi with no camera listed: only `camera` blocks, exit 1.
4. Simulated Pi with `VISIO_BATTERY_SOURCE=pijuice` and no HAT: `pijuice` and `i2c` warn, exit 0.

The daemon's capture, mux, upload and flag paths run against fakes in the firmware suite, and the firmware-upload to pipeline-ingest handoff runs against live local Supabase in `integration/tests/test_storage_ingestion_live.py`.
Nothing here replaces the physical checklist in the PR body.

## 2026-11-17: second merge of origin/main (b809807)

Merge commit `d2344fe` brings #68-#77 and #83 into the branch as a plain two-parent merge (no rebase, no force push).
Since the previous merge used a real merge commit, the merge base was correct this time (`ffed638`), and git reported six conflicting files.
Logs and scripts for this round are in [2026-11-17/](2026-11-17/).

### Conflicts and resolutions

| File | Branch side | Main side | Resolution |
|---|---|---|---|
| `firmware/visio_recorder/daemon.py` | boot drain count seeds `segments_uploaded_at_start` | #70: flush, recover orphans, flush | Both: #70's flush, recover, flush order, with both flushes summed into `segments_uploaded_at_start`. Preflight still runs first (ExecStartPre, then again at the top of `main()`), and `VISIO_BATTERY_SOURCE` still picks the battery reader. |
| `firmware/visio_recorder/daemon.py` (semantic, no textual conflict) | worker seeds its counter from `segments_uploaded_at_start` | #70: `recording_active` start/stop pushes report a hard-coded 0 until the worker runs | The start push and the stop push now carry `segments_uploaded_at_start`, so a boot backlog (or a stop before the first segment) is not reported to the app as 0. New test `test_boot_drain_count_survives_a_stop_before_any_segment`; the existing seed test now expects `[7, 8, 8]`. Both fail with the auto-merged code. |
| `firmware/visio_recorder/supabase_clients.py` | `x-upsert: true` makes a retried upload idempotent | #77/#83: look the object up first, same size counts as uploaded, a different size raises, never overwrite; stream the file | Main's. It solves the same power-loss retry without letting a repeated timestamp key overwrite a different segment, and streams instead of reading whole. |
| `firmware/tests/test_supabase_clients.py` | upsert-based fake, power-loss drain test | size-aware fake with `info`/`list` | Main's file, plus the branch's end-to-end power-loss test (`test_a_retry_after_power_loss_between_upload_and_unlink_still_drains`) ported to main's fake; it now also asserts that the already-stored segment is not sent again. The branch's test pinning upsert behaviour was dropped since nothing upserts now. |
| `AGENTS.md`, `README.md` | shellcheck gate, stacked-branch trigger notes | prettier gate, db-types drift check, trixie Python floor wording | Both, combined. |
| `docs/ARCHITECTURE.html` | manifest for the branch's specs | manifest for main's specs | Regenerated (see below). |

Also changed in the merge, without textual conflicts:

- `docs/superpowers/specs/2026-07-04-visio-pendant-design.md`: the startup sequence now names the ExecStartPre/in-`main()` preflight, `VISIO_BATTERY_SOURCE`, and the seeded upload counter, and the retry bullet describes the size-checked upload instead of `x-upsert`.
- `upload_queue.py` and `test_flag_button.py` comments that cited `x-upsert` now describe the size check.
- `lint.yml`: the branch's shellcheck job moved to `actions/checkout@v7` like the rest of #73, and lints `docs/superpowers/evidence/dependabot-triage/seed-screens.sh`, the shebang script main added (clean).
- `app/jest.config.js` / `jest.setup.js` auto-merged to the branch's `testTimeout: 45000` and `asyncUtilTimeout: 30000` while keeping main's node-fetch transform (#75). The higher values are still needed: on this cold-cache run two tests took 16115 ms and 15512 ms, both over main's 15000 ms `testTimeout` ([app-jest-cold-cache.log](2026-11-17/app-jest-cold-cache.log)).

### ARCHITECTURE.html

Regenerated per `docs/architecture-regeneration.md`.
The startup diagram adds the ExecStartPre preflight, the in-`main()` re-check, and the battery-source reader before #70's flush, recover, flush nodes, and the recording loop node notes the seeded count and the initial `recording_active=true` push.
The upload bullet describes the size-checked retry.
All three manifest lines pass `sha256sum -c` ([hooks.log](2026-11-17/hooks.log)); rendered diagram: [architecture-startup-diagram.png](2026-11-17/architecture-startup-diagram.png).

### Test evidence (merged tree, local Supabase CLI 2.109.0, reused running stack)

| Surface | Result | Log |
|---|---|---|
| ruff check, ruff format --check, mypy (firmware, pipeline, integration) | clean | [firmware](2026-11-17/python-lint-firmware.log), [pipeline](2026-11-17/python-lint-pipeline.log), [integration](2026-11-17/python-lint-integration.log) |
| firmware pytest | 309 passed | [firmware-pytest.log](2026-11-17/firmware-pytest.log) |
| pipeline pytest | 163 passed | [pipeline-pytest.log](2026-11-17/pipeline-pytest.log) |
| pgTAP (`supabase test db`) | 9 files, 59 tests, PASS | [pgtap.log](2026-11-17/pgtap.log) |
| integration pytest (live Supabase) | 11 passed, 0 skipped | [integration-pytest.log](2026-11-17/integration-pytest.log) |
| generated db types drift | no drift | [gen-db-types.log](2026-11-17/gen-db-types.log) |
| app `tsc --noEmit`, eslint, prettier `format:check`, typecheck | all exit 0 | [app-tsc-lint-prettier.log](2026-11-17/app-tsc-lint-prettier.log) |
| app jest, cold cache, live Supabase | 33 suites, 130 tests passed | [app-jest-cold-cache.log](2026-11-17/app-jest-cold-cache.log) |
| app web export | exit 0 | [app-web-export.log](2026-11-17/app-web-export.log) |
| shellcheck (lint.yml list) | clean | [shellcheck.log](2026-11-17/shellcheck.log) |
| pre-commit hook suite + manifest | 16/16, manifest OK | [hooks.log](2026-11-17/hooks.log) |

### Firmware with hardware simulated

The same four [simulate-preflight.sh](simulate-preflight.sh) scenarios on the merged tree give the same outcomes: bare host exit 1, simulated Pi exit 0, no camera exit 1, pijuice without a HAT exit 0 ([log](2026-11-17/firmware-preflight-simulated-hardware.log)).

[simulate-boot.sh](2026-11-17/simulate-boot.sh) boots the merged daemon the way the systemd unit does.
It runs ExecStartPre (`python -m visio_recorder.preflight`) and starts the daemon only on exit 0.
The daemon then uses real local Supabase (auth session, `devices`, `device_status`, `segments` bucket) and real ffmpeg muxing.
A stub `rpicam-vid` writes a 2 s H.264 test pattern, and logging fakes stand in for the LED and flag button ([simulate-boot.py](2026-11-17/simulate-boot.py)).
It plants one queued segment, a lone `.h264` and a lone `.mp4`, then sends SIGTERM after two live segments are stored.
From [simulated-boot.log](2026-11-17/simulated-boot.log):

1. ExecStartPre: all checks `[OK]`, exit 0; `main()` re-runs preflight, also clean.
2. `flush_pending` uploads the queued segment (1).
3. `recover_orphaned_segments` muxes the lone `.h264` and queues both orphans; the second `flush_pending` uploads them (2).
4. `run_recording_loop` starts: the first `device_status` row is `recording_active: true, segments_uploaded_today: 3` (seeded by the boot drains), then 4, 5 as segments upload.
5. After SIGTERM the final row is `recording_active: false, segments_uploaded_today: 6`, `main()` returns 0, the queue is empty, and only `device.json`, `device_registered` and `session.json` remain in the data dir.

Nothing here replaces the physical checklist in the PR body.
