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
docs/
  superpowers/
    specs/           -- approved design spec
    plans/           -- executable implementation plans (one per epic)
```

The `pipeline/` and `app/` subsystems from the spec's repository structure are planned but not yet implemented (Epics 2-3).

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
