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
