# vision

Visio pendant - a wearable camera that records your day and turns it into a nightly highlight reel.

**Design spec:** [`docs/superpowers/specs/2026-07-04-visio-pendant-design.md`](docs/superpowers/specs/2026-07-04-visio-pendant-design.md)
**Roadmap:** [`docs/superpowers/plans/2026-07-04-visio-epics-overview.md`](docs/superpowers/plans/2026-07-04-visio-epics-overview.md)

## Repository layout

```
supabase/            -- Epic 0: Postgres migrations, pgTAP tests, local stack config
  migrations/        -- schema, RLS policies, storage buckets
  tests/database/    -- pgTAP test suites run via `supabase test db`
firmware/            -- Epic 1: visio-recorder Python systemd daemon
  visio_recorder/    -- battery, LED, muxer, upload queue, uploader, wifi/device onboarding, recording loop
  tests/             -- pytest suite (fakes for every hardware/network Protocol)
  systemd/           -- installable systemd unit
pipeline/            -- Epic 2: nightly cloud AI pipeline (Python package)
  pipeline/          -- ingestion, scoring, selection, assembly, delivery stages,
                        nightly orchestrator + `python -m pipeline` entrypoint
  pipeline/adapters/ -- real Supabase/LiteLLM/Expo/FFmpeg clients (wired only in __main__)
  tests/             -- pytest suites run via `uv run --extra dev pytest`
app/                 -- Epic 3: React Native (Expo) mobile companion app
  src/logic/         -- pure TypeScript calculation (no React imports)
  src/hooks/         -- Supabase data hooks (client injected for testing)
  src/containers/    -- wires hooks to presentational screens/components
  src/screens/       -- presentational screens (props in, JSX out)
  src/components/    -- presentational components (timeline, segment preview, reel player)
  src/theme.ts       -- shared dark theme for StyleSheet styling
  __tests__/         -- Jest suites run via `npm test`
integration/         -- Epic 5: cross-service integration suite (Python package)
  tests/             -- pytest suites running firmware + pipeline + app's real code
                        against each other, and against a live local Supabase
docs/
  superpowers/
    specs/           -- approved design specs (rendered by docs/ARCHITECTURE.html)
    plans/           -- executable implementation plans (one per epic)
```

All three software subsystems (Epics 1-3) are implemented, including the Epic 5 device/UI wiring deferred by each plan's Handoff section: the pipeline's nightly orchestrator (issue #5), the firmware's daemon glue (issue #7), and the app's screen wiring (issue #8).

## Architecture doc

[`docs/ARCHITECTURE.html`](docs/ARCHITECTURE.html) is a living, visual rendering of the design specs - open it directly in a browser.
It is generated from every spec under `docs/superpowers/specs/`; edit the specs, then see [`docs/architecture-regeneration.md`](docs/architecture-regeneration.md) to regenerate the page.
The page embeds one `<!-- source-sha256: <hash>  <spec path> -->` comment per spec it renders, and a pre-commit hook blocks any commit that changes a spec without updating that spec's line - run this once per clone (worktrees share it):

```sh
git config core.hooksPath scripts/hooks
```

The hook's own suite is [`scripts/hooks/tests/test-pre-commit.sh`](scripts/hooks/tests/test-pre-commit.sh) (plain `git` + `sha256sum`, no test framework); [`.github/workflows/hooks.yml`](.github/workflows/hooks.yml) runs it on every PR and also re-verifies the committed manifest against the specs on disk.

## Continuous integration

[`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs all three test suites on every pull request targeting `main` and every push to `main`.
The firmware and pipeline jobs run `uv run --locked --extra dev pytest` on Python 3.11 (the Raspberry Pi OS Bookworm device target); `--locked` enforces the committed `uv.lock`.
The app job runs `npm ci`, `npx tsc --noEmit`, and `npx jest --ci` on Node 22.
The `npm ci` step raises npm's fetch retries to 5 with 10-60s backoff to ride out transient registry failures ([#12](https://github.com/vietwillnguyen/vision/issues/12)); the settings are scoped to that step's env rather than a committed `.npmrc`, so local `npm install` keeps npm defaults.
The integration job additionally runs a `Run pgTAP database tests` step - `supabase test db` against the local Supabase instance that job already starts - gating the database contract (RLS policies, table grants, and storage bucket policies) defined by the nine suites in [`supabase/tests/database/`](supabase/tests/database).
It is a step inside that job rather than a job of its own because the suites finish in about a second, so a dedicated job would spend a third `supabase start` to save nothing, and because a new job would have to be added as a required status check in branch protection before it actually gated anything, whereas a step inherits the existing job's protection immediately.
The step runs before the pytest step: each suite wraps itself in `begin`/`rollback`, so it leaves the freshly migrated database untouched for the tests that follow.
The Supabase CLI is pinned through the workflow-level `SUPABASE_CLI_VERSION` env var rather than `version: latest`, so CI cannot change underneath a commit; Dependabot bumps action refs but not action inputs, so that pin is a manual bump.

## Lint, format, and type-check

[`.github/workflows/lint.yml`](.github/workflows/lint.yml) gates the same commands on every pull request targeting `main` and every push to `main`.
It needs neither Docker nor a Supabase instance.

Python uses [ruff](https://docs.astral.sh/ruff/) for both linting and formatting (it replaces black, flake8, and isort) and [mypy](https://mypy.readthedocs.io/) for type checking.
The rule set is shared: the repo-root [`ruff.toml`](ruff.toml) holds it, and each package's `pyproject.toml` extends it.
Run all three from any of `firmware/`, `pipeline/`, or `integration/`:

```bash
uv run --extra dev ruff check .           # lint
uv run --extra dev ruff check --fix .     # lint, fixing what is auto-fixable
uv run --extra dev ruff format .          # format (--check to verify only)
uv run --extra dev mypy                   # type-check (paths come from pyproject.toml)
```

The app uses ESLint with [`eslint-config-expo`](https://docs.expo.dev/guides/using-eslint/) and `tsc`:

```bash
cd app
npm run lint        # eslint, warnings included (--max-warnings 0)
npm run lint -- --fix
npm run typecheck   # tsc --noEmit
```

## Supabase foundation (Epic 0)

The shared database contract every other subsystem codes against:

- Tables: `devices`, `device_status`, `segments`, `reels`, `score_weights` (score weight defaults 0.4/0.3/0.2), and (added post-Epic-0 for issue #5) `pipeline_dlq`, the nightly pipeline's service_role-only dead-letter queue; `reels` is unique on `(device_id, date)` so same-day pipeline re-runs upsert.
- `devices.push_token` (nullable) stores the mobile app's Expo push token for nightly reel notifications.
- Row-level security on every table: rows are visible only to the owning user (`auth.uid()` matched directly or via the owning device's `user_id`).
- Private `segments` and `reels` storage buckets with owner-scoped object policies keyed on the `{device_id}/` path prefix.
- Table privileges are granted explicitly: `service_role` gets full DML, `authenticated` gets select everywhere, writes only on `devices`, `score_weights`, `segments.user_feedback`, and (added post-Epic-0, see `20260710090000_grant_device_status_writes.sql`) insert/update on `device_status` - the firmware daemon never holds a `service_role` key, so it upserts its own status row as `authenticated`. The `anon` role gets no grants - devices are paired to authenticated users only.

### Local development

Requires the [Supabase CLI](https://supabase.com/docs/guides/local-development) (2.x) and Docker.

```bash
supabase start      # start the local stack
supabase db reset   # recreate the database from migrations
supabase test db    # run the pgTAP test suites
```

The stack `supabase start` boots is trimmed: Studio, analytics, the edge runtime, and vector storage are disabled in [`supabase/config.toml`](supabase/config.toml), each with an inline comment recording why it is unused here.
Flip `[studio]` back on locally if you want the table editor.

## Firmware (Epic 1)

**Plan:** [`docs/superpowers/plans/2026-07-04-visio-firmware.md`](docs/superpowers/plans/2026-07-04-visio-firmware.md)

The `visio-recorder` Python systemd daemon: boot-time battery check, WiFi + Supabase auth onboarding via QR code, rolling 5-minute H.264-to-MP4 segment capture and upload, an LED state machine, and per-segment `device_status` reporting.
The manual flag marker (`FLAG_YYYYMMDD_HHMMSS.marker`, `flag_button.py`) is wired to a GPIO 17 button press in `main()`: a debounced press hands the marker to a dedicated flag-upload worker thread, which uploads it immediately or leaves it queued for the next boot's flush on failure.
Every hardware or network boundary (PiJuice, GPIO, WS2812B LEDs, `ffmpeg`/`rpicam-vid`, Supabase) is a small `Protocol` with a fake used in tests; `daemon.py` is the only module that wires real implementations together.
On boot it runs the battery/LED startup sequence, scans a QR code to onboard WiFi (writing a NetworkManager keyfile) and Supabase auth on first boot, activates the NetworkManager connection (`nmcli connection reload` + `up`, retried best-effort on restart boots), registers the device, flushes any queued uploads left over from a previous run, then starts the per-segment `rpicam-vid` capture loop with the flag-button listener and a background upload worker with real disk-usage stats.
Configuration is read from environment variables via the systemd unit's `EnvironmentFile=/etc/visio-recorder.env`: `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `VISIO_DATA_DIR`, `VISIO_SEGMENT_DURATION_MS`, `VISIO_FRAMERATE`.
Device prerequisites beyond this project's `uv.lock`: `apt install zbar-tools` for QR decoding, plus the `pijuice`, `rpi_ws281x`, and `gpiozero` system packages for the battery, LED, and flag-button drivers on the Pi.

### Local development

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). The committed `uv.lock` is the reproducibility contract for firmware deployed to devices.

```bash
cd firmware
uv sync --extra dev   # install dependencies from uv.lock
uv run pytest         # run the test suite
```

## Cloud AI pipeline (Epic 2)

The unit-tested core of the nightly job that turns a day's segments into a highlight reel.
Every stage is a pure function or takes an injectable client `Protocol`, so the test suite never calls a real LLM, FFmpeg, or Expo:

- Ingestion (`pipeline/ingestion.py`): builds `segments` rows from storage object keys (`YYYYMMDD_HHMMSS.mp4`) and applies manual flag markers (`FLAG_YYYYMMDD_HHMMSS.marker`). Unparseable or out-of-device-prefix keys are returned as rejects, and markers matching no segment window as unmatched, for DLQ-style retry by the orchestrator.
- Scoring (`pipeline/scoring/`): audio activity from transcription features, motion intensity from frame diffs (low-motion segments skip vision scoring as a cost gate), and scene novelty from a vision LLM routed through LiteLLM (Claude Haiku default, `json_schema` structured outputs). Combined by the composite formula: weighted sum (default weights 0.4/0.3/0.2), times 1.5 when manually flagged.
- Selection (`pipeline/selection/`): fills a 90s target with 15s clips, always includes manually flagged segments, and never places two same-location clips back to back.
- Assembly (`pipeline/assembly/`): builds the FFmpeg trim and concat commands (720p, optional vintage filter).
- Delivery (`pipeline/delivery/`): Expo push notification when the reel is ready.

The nightly orchestrator (`pipeline/orchestrator.py`) wires these stages per device against injectable boundaries, with real adapters in `pipeline/adapters/` (Supabase tables + storage via the service_role key, LiteLLM vision and Whisper transcription, Expo push, FFmpeg media probing).
Rejected keys and unmatched flag markers land in the `pipeline_dlq` table for nightly retry with attempt counts; a key that exhausts its retries is escalated and the device owner is notified (issue #5's DLQ policy).
It runs as the [`nightly-reel`](.github/workflows/nightly-reel.yml) GitHub Actions workflow (nightly cron, or `workflow_dispatch` with an optional `day` input for the Epic 5 manual trigger), and locally as `uv run python -m pipeline [--day YYYY-MM-DD]` with `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` set, plus the LiteLLM provider keys for the configured models (`ANTHROPIC_API_KEY` for vision, `OPENAI_API_KEY` for Whisper transcription; optional overrides: `VISIO_VISION_MODEL`, `VISIO_TRANSCRIPTION_MODEL`, `VISIO_TARGET_DURATION_SEC`, `VISIO_VINTAGE`).

### Local development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
cd pipeline
uv run --extra dev pytest   # run the unit test suite
```

## Mobile companion app (Epic 3 + Epic 5 screen wiring)

The React Native (Expo, TypeScript) app: a bottom tab navigator (`@react-navigation/bottom-tabs`) composing Today's Reel, Raw Footage, Device, and Archive behind real Supabase Auth (sign-in, session restore, and `@react-native-async-storage/async-storage` session persistence), backed by `src/logic/` calculation, `src/hooks/` data fetching, and `src/containers/` wiring the two together to presentational screens/components.

- Strict layering: `src/logic/` is pure TypeScript with zero React imports; hooks take an injected `SupabaseClient` as their first parameter (tested with a fake client, never a real network call, and never imported as a module-level singleton outside `App.tsx` and `src/lib/supabase.ts`); `src/containers/` wire hooks to presentational screens/components.
- Video playback uses `expo-video` (registered as a config plugin in `app.json`), not the deprecated `expo-av`; timeline thumbnails are generated and cached per-segment via `expo-video-thumbnails`.
- `useDeviceStatus` exposes a discriminated `loading | error | ready` state and a `subscribe()` status callback so realtime channel errors (`CHANNEL_ERROR`/`TIMED_OUT`/`CLOSED`) surface as a stale banner instead of silently-stale data.
- Archive heat-map ranges are normalized to UTC midnight via `src/logic/dates.ts` (30-day inclusive range), avoiding the off-by-one the original plan's Handoff section warned about for users west of UTC.
- Styling uses `StyleSheet` with a shared dark theme (`src/theme.ts`) and an `accessibilityLabel` on every interactive element.
- The Device tab nests a `@react-navigation/native-stack` (`DeviceStack`): the Re-onboard button navigates to a form + QR screen (`react-native-qrcode-svg`/`react-native-svg`) that calls `client.auth.getSession()` and encodes `{ssid, password, user_access_token, user_refresh_token}` - the exact shape firmware's `parse_onboarding_qr_payload` expects.
- Out of scope for this wiring pass (tracked separately, not silently dropped): the regenerate bottom sheet (`validateRegenerateRequest` has no backend consumer yet).
- The Supabase client reads `EXPO_PUBLIC_SUPABASE_URL` and `EXPO_PUBLIC_SUPABASE_ANON_KEY` from `app/.env` (gitignored).
- Native-only modules with no web implementation (`expo-video-thumbnails`, `expo-file-system`'s `File`/`Paths`, `expo-media-library`) are wrapped in `src/lib/videoThumbnails.{ts,web.ts}` and `src/lib/mediaSave.{ts,web.ts}` - Metro resolves the `.web.ts` variant (an `isAvailable: false` stub) on web instead of the native file, so the web bundle never evaluates a native-only import. `react-native-web`/`react-dom` are `devDependencies`, used only for local web preview; CI runs `npx expo export --platform web` as a smoke check so a reintroduced top-level native import fails the build instead of only being catchable by a manual web run.

### Local development

```bash
cd app
npm install           # install dependencies
npm test              # run the Jest test suites
npx expo start        # launch the Expo dev server
npx expo start --web  # preview in a browser (layout/styling only - no native video playback)
```

Lint and type-check commands are in [Lint, format, and type-check](#lint-format-and-type-check).

## Cross-service integration suite (Epic 5)

A fourth Python package whose tests run each subsystem's *real* code against the others, rather than against each subsystem's private fakes: firmware's marker/segment writers against pipeline's parsers, pipeline's real row shapes against the migrations and the app's `mapReelRow()`, and pipeline's real `SupabaseStore` plus firmware's real uploader against a live local Supabase instance for the RLS and storage-ingestion handoffs.
Tests that need a live instance skip themselves when the `SUPABASE_*` env vars are unset, so the suite is runnable without Docker.
`.github/workflows/tests.yml`'s `integration` job starts a real Supabase instance, so those tests actually run (not skip) on every PR.

See [`integration/README.md`](integration/README.md) for what each test file proves and why, including the one integration test that lives in the `app` package instead (Realtime channel-error handling).

### Local development

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
cd integration
uv sync --extra dev   # install dependencies from uv.lock
uv run pytest         # run the suite (live-Supabase tests skip without SUPABASE_* set)
```
