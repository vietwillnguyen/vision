# Cross-service integration suite

Implements the plan in `.lavish/test-validation-plan.html`: tests that run
each subsystem's real code against each other, not each subsystem's private
mocks.

## What's here

`tests/test_marker_contract.py` proves firmware's segment/marker *writers*
and pipeline's `pipeline.ingestion` *parsers* agree on filename format, by
importing both real packages and round-tripping values through them. No
Supabase instance is required for these.

`tests/test_row_schema_contract.py` runs the real
`pipeline.orchestrator.run_nightly` with only the plan's named external-cost
boundaries faked (media probe, transcriber, vision, ffmpeg runner, push -
same carve-out as the plan's Stage 2), then checks the real `segments`/`reels`
row dicts it produces against two independent real sources of truth parsed
straight from the repo: the `create table` column lists in the supabase
migrations, and the `row.<field>` accesses in `app/src/hooks/useReel.ts`'s
`mapReelRow()`. This covers two of the plan's "Now covered locally" bullets
("Segment row shape ... matches what pipeline queries" and "Reel row ...
matches what app's hooks expect") without a live Postgres instance, since
`persist_segments`/`insert_reel` are faked at the store boundary rather than
against a database - a schema rename on either side (migration column or
`mapReelRow` field) breaks this test.

`tests/test_rls_end_to_end.py` runs pipeline's real `SupabaseStore` (the
same class the orchestrator uses in production) against a live local
Supabase instance: writes a segment/reel row as `service_role` (pipeline's
write path), then signs in as two real `auth.users` - the device owner and
an unrelated user - and reads the same rows back through `SupabaseStore` as
each. This exercises `segments_owner_access`/`reels_owner_access`
(`supabase/migrations/20260704120500_rls_policies.sql`) as real Postgres
policy decisions on a real JWT, not a pgTAP per-assertion check, covering
the plan's "RLS still permits each real service-role/authenticated flow end
to end" bullet. It needs `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and
`SUPABASE_SERVICE_ROLE_KEY` pointing at a running `supabase start` instance
(`conftest.py`'s `live_supabase_env` fixture skips the test automatically
when they're unset, e.g. in this sandbox where Docker is unavailable).

This suite runs in CI: `.github/workflows/tests.yml`'s `integration` job
starts a local Supabase instance via `supabase/setup-cli` + `supabase
start`, exports its connection details into `SUPABASE_*` env vars, then runs
`uv run --locked --extra dev pytest` here on every PR - GitHub-hosted
runners have Docker preinstalled, so this isn't blocked the way local
iteration in this sandbox is. Per the plan's "Decided: CI gating" item, this
means the RLS test above actually runs (not just skips) on every PR even
though it can't be run locally here.

## Realtime channel-error handling (app package)

The plan's last "Now covered locally" bullet - Realtime channel-error
handling against a real Postgres changes feed, not a mocked subscription -
lives outside this Python package, in
`app/__tests__/hooks/useDeviceStatus.live.test.tsx`. It runs `app`'s real
`@supabase/supabase-js` `createClient()` (the same call as
`app/src/lib/supabase.ts`) against a live local Supabase instance: an
authenticated device owner subscribes through the real `useDeviceStatus`
hook, then a second real client upserts that owner's `device_status` row
(the firmware daemon's write path, granted by
`supabase/migrations/20260710090000_grant_device_status_writes.sql`), and the
test asserts the hook's `realtime` field reaches `'live'` and its `status`
reflects the row over the wire - not a mocked channel callback like every
other `useDeviceStatus` test. It reads the same `SUPABASE_URL` /
`SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_ROLE_KEY` env vars as this package's
`conftest.py` and skips automatically when they're unset. `.github/workflows/tests.yml`'s
`app` job now starts and stops a local Supabase instance the same way the
`integration` job does, so this test actually runs (not just skips) in CI.

The plan's Stage 1 (firmware's upload adapter writing a real `segments` row)
turned out not to map to reality: firmware only uploads bytes to Storage and
never inserts a `segments` row itself (see `test_row_schema_contract.py`'s
docstring/learnings) - pipeline is the sole writer of that row via
`SupabaseStore.persist_segments`. But firmware's real upload *does* feed
pipeline's real listing: `tests/test_storage_ingestion_live.py` signs in as a
real device owner, calls firmware's actual
`visio_recorder.uploader.upload_segment` to write a stub MP4 into the live
`segments` bucket (gated by `segments_bucket_owner_access` in
`supabase/migrations/20260704120600_storage_buckets.sql`), then runs
pipeline's real `SupabaseStore.list_segment_object_keys` and
`pipeline.ingestion.build_segments_from_object_keys` against that same
bucket to prove the object firmware wrote round-trips into the segment
pipeline actually parses - the real Stage 1 -> Stage 2 handoff, storage
object to parsed `Segment`, rather than a bypassed direct DB write. Same
live-instance requirement and skip behavior as `test_rls_end_to_end.py`.

## Running

```
cd integration
uv sync --extra dev
uv run pytest
```

To also run the RLS test locally, start Supabase in `../supabase` first
and export its connection details:

```
cd supabase && supabase start
eval "$(supabase status -o env)"
SUPABASE_URL="$API_URL" SUPABASE_ANON_KEY="$ANON_KEY" \
  SUPABASE_SERVICE_ROLE_KEY="$SERVICE_ROLE_KEY" \
  uv run --project ../integration pytest ../integration
```
