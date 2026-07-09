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
  pipeline/          -- ingestion, scoring, selection, assembly, delivery stages
  tests/             -- pytest suites run via `uv run --extra dev pytest`
app/                 -- Epic 3: React Native (Expo) mobile companion app
  src/logic/         -- pure TypeScript calculation (no React imports)
  src/hooks/         -- Supabase data hooks (client injected for testing)
  src/screens/       -- presentational screens (props in, JSX out)
  src/components/    -- presentational components (timeline, segment preview)
  __tests__/         -- Jest suites run via `npm test`
docs/
  superpowers/
    specs/           -- approved design spec
    plans/           -- executable implementation plans (one per epic)
```

All three software subsystems (Epics 1-3) are implemented; their remaining device/UI wiring is deferred to Epic 5 per each plan's Handoff section.

## Continuous integration

[`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs all three test suites on every pull request targeting `main` and every push to `main`.
The firmware and pipeline jobs run `uv run --locked --extra dev pytest` on Python 3.11 (the Raspberry Pi OS Bookworm device target); `--locked` enforces the committed `uv.lock`.
The app job runs `npm ci`, `npx tsc --noEmit`, and `npx jest --ci` on Node 22.

## Supabase foundation (Epic 0)

The shared database contract every other subsystem codes against:

- Tables: `devices`, `device_status`, `segments`, `reels`, `score_weights` (score weight defaults 0.4/0.3/0.2).
- `devices.push_token` (nullable) stores the mobile app's Expo push token for nightly reel notifications.
- Row-level security on every table: rows are visible only to the owning user (`auth.uid()` matched directly or via the owning device's `user_id`).
- Private `segments` and `reels` storage buckets with owner-scoped object policies keyed on the `{device_id}/` path prefix.
- Table privileges are granted explicitly: `service_role` gets full DML, `authenticated` gets select everywhere, writes only on `devices`, `score_weights`, and `segments.user_feedback`. The `anon` role gets no grants - devices are paired to authenticated users only.

### Local development

Requires the [Supabase CLI](https://supabase.com/docs/guides/local-development) (2.x) and Docker.

```bash
supabase start      # start the local stack
supabase db reset   # recreate the database from migrations
supabase test db    # run the pgTAP test suites
```

## Firmware (Epic 1)

**Plan:** [`docs/superpowers/plans/2026-07-04-visio-firmware.md`](docs/superpowers/plans/2026-07-04-visio-firmware.md)

The `visio-recorder` Python systemd daemon: boot-time battery check, WiFi + Supabase auth onboarding via QR code, rolling 5-minute H.264-to-MP4 segment capture and upload, a manual flag button, an LED state machine, and per-segment `device_status` reporting. Every hardware or network boundary (PiJuice, GPIO, WS2812B LEDs, `ffmpeg`/`rpicam-vid`, Supabase) is a small `Protocol` with a fake used in tests; `daemon.py` is the only module that wires real implementations together, and currently only implements the startup battery/LED sequence - the remaining wiring (process supervision, threading, real disk-usage stats) is deferred to Epic 5 (see the plan's Handoff section).

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

The nightly orchestrator that wires these stages against real Supabase/LiteLLM/Expo clients is follow-up work - see the Handoff section of [`docs/superpowers/plans/2026-07-04-visio-pipeline.md`](docs/superpowers/plans/2026-07-04-visio-pipeline.md).

### Local development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
cd pipeline
uv run --extra dev pytest   # run the unit test suite
```

## Mobile companion app (Epic 3)

The React Native (Expo, TypeScript) app foundation: auth form validation, regenerate-preferences validation, timeline bucketing, archive heat-map cells, segment preview/export, and a realtime device-status hook.

- Strict layering: `src/logic/` is pure TypeScript with zero React imports, hooks take an injected `SupabaseClient` (tested with a fake client, never a real network call), and screens/components are presentational.
- Screen wiring is deferred to Epic 5 per the plan's Handoff section: the bottom tab navigator, container screens, real Supabase Auth, video playback, and timeline thumbnails.
- The Supabase client reads `EXPO_PUBLIC_SUPABASE_URL` and `EXPO_PUBLIC_SUPABASE_ANON_KEY` from `app/.env` (gitignored).

### Local development

```bash
cd app
npm install         # install dependencies
npm test            # run the Jest test suites
npx tsc --noEmit    # type-check
npx expo start      # launch the Expo dev server
```
