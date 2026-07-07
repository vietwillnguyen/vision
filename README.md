# vision

Visio pendant - a wearable camera that records your day and turns it into a nightly highlight reel.

**Design spec:** [`docs/superpowers/specs/2026-07-04-visio-pendant-design.md`](docs/superpowers/specs/2026-07-04-visio-pendant-design.md)
**Roadmap:** [`docs/superpowers/plans/2026-07-04-visio-epics-overview.md`](docs/superpowers/plans/2026-07-04-visio-epics-overview.md)

## Repository layout

```
supabase/            -- Epic 0: Postgres migrations, pgTAP tests, local stack config
  migrations/        -- schema, RLS policies, storage buckets
  tests/database/    -- pgTAP test suites run via `supabase test db`
pipeline/            -- Epic 2: nightly cloud AI pipeline (Python package)
  pipeline/          -- ingestion, scoring, selection, assembly, delivery stages
  tests/             -- pytest suites run via `uv run --extra dev pytest`
docs/
  superpowers/
    specs/           -- approved design spec
    plans/           -- executable implementation plans (one per epic)
```

The `firmware/` and `app/` subsystems from the spec's repository structure are planned but not yet implemented (Epics 1 and 3).

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

## Cloud AI pipeline (Epic 2)

The unit-tested core of the nightly job that turns a day's segments into a highlight reel.
Every stage is a pure function or takes an injectable client `Protocol`, so the test suite never calls a real LLM, FFmpeg, or Expo:

- Ingestion (`pipeline/ingestion.py`): builds `segments` rows from storage object keys (`YYYYMMDD_HHMMSS.mp4`) and applies manual flag markers (`FLAG_HHMMSS.marker`). Unparseable or out-of-device-prefix keys are returned as rejects, and markers matching no segment window as unmatched, for DLQ-style retry by the orchestrator.
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
