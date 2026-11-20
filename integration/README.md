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
row dicts it produces against `tests/fixtures/public_schema.json`: the public
schema's `information_schema.columns`, read out of a live local Supabase
instance by `scripts/gen-db-types.sh`. Because `persist_segments`/`insert_reel`
are faked at the store boundary, this needs no live instance of its own, and a
migration that adds a required column or renames one the pipeline writes breaks
it.

It covers only the pipeline half of the plan's "Segment row shape ... matches
what pipeline queries" bullet. The app half - "Reel row ... matches what app's
hooks expect" - is no longer a Python test: `app/src/lib/supabase.ts` builds its
client as `createClient<Database>` over the generated
`app/src/generated/database.ts`, so every `.from(...).select(...)` in the app is
typed against the real column list and `npx tsc --noEmit` rejects a field the
schema does not have. That gates the whole app rather than the single hook the
previous regex-based version of this test knew how to read. Both the generated
types and the fixture above come from the same script and the same instance, and
the `app` job in `.github/workflows/tests.yml` regenerates them and fails on any
diff, so neither can drift away from the migrations.

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

`tests/test_row_schema_contract.py` also checks the pairing RPCs: the fixture's
`functions` section (from `information_schema.routines`) must carry
`create_pairing_code`, `claim_pairing` and `unpair_device` with their argument
names, and firmware's `CLAIM_RPC`/`CLAIM_ARG` must match `claim_pairing`.
Firmware has no generated client, so this is its "migration changed, build goes
red" check.

`tests/test_device_session_live.py` pairs a pendant through the real flow
(`tests/pendant.py`: the owner's client calls `create_pairing_code`, the v2 QR
payload goes through firmware's parser, and firmware's `complete_pairing` signs
in anonymously and claims the code) and proves the pendant keeps its own login
when the phone refreshes or logs out. It also pins what that login may and may
not do, that "Remove device" and a re-pair revoke it, and that a used code or an
unpaired anonymous user gets nothing. Code expiry cannot be aged through the
public API, so the pgTAP suite covers it.
A pendant booting before NTP sync is simulated cheaply: the test re-signs its
access token with a past `exp` using the stack's legacy HS256 secret
(`SUPABASE_JWT_SECRET`, from `JWT_SECRET` in `supabase status -o env`) and sets
supabase-py's clock two hours back, then boots it.
`tests/test_offline_refresh_live.py` puts the pendant behind a TCP link it can
cut (`tests/network.py`) and simulates an hour off Wi-Fi the same way: an
expired copy of the access token, firmware's clock moved forward, and
supabase_auth's refresh timer made to fall due during the outage. It proves
the next upload and status write succeed once the link is back (issue #134).
`tests/test_firmware_upload_live.py` and `tests/test_storage_ingestion_live.py`
upload through the same `paired_device` fixture (`conftest.py`), because only
the pendant's own login may write to storage. The local stack enables
anonymous sign-ins and raises their per-IP rate limit for this
(`supabase/config.toml`).

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
hook, then a pendant paired the way firmware pairs (an anonymous sign-in that
claims the owner's pairing code, see
`supabase/migrations/20261119120000_device_pairing.sql`) upserts the device's
`device_status` row under its own login, and the test asserts the hook's `realtime`
field reaches `'live'` and its `status`
reflects the row over the wire - not a mocked channel callback like every
other `useDeviceStatus` test. It reads the same `SUPABASE_URL` /
`SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_ROLE_KEY` env vars as this package's
`conftest.py` and skips automatically when they're unset. `.github/workflows/tests.yml`'s
`app` job now starts and stops a local Supabase instance the same way the
`integration` job does, so this test actually runs (not just skips) in CI.

This test seeds an initial `device_status` row before rendering the hook:
`useDeviceStatus` only exposes its `realtime` field once its own initial
fetch resolves to `kind: 'ready'`, so with no row at all it stays
`{ kind: 'loading' }` forever and `realtime` never appears in the returned
object, no matter the actual channel status. It also requires
`public.device_status` to be added to the `supabase_realtime` publication
(see `supabase/migrations/20260720120000_enable_device_status_realtime.sql`)
- without that, Supabase Realtime never streams `postgres_changes` for the
table at all, and the channel silently never reaches `SUBSCRIBED`. The Jest
run itself needs a real `fetch` (jest-expo's React Native XHR-based polyfill
returns a broken response against a plain `http://` localhost URL, so the
test passes `node-fetch` via `createClient`'s `global.fetch` option); the
`WebSocket` global `@supabase/realtime-js` requires is Node 22's native one,
which jest-expo leaves in place, so no `ws` shim is needed.

The plan's Stage 1 (firmware's upload adapter writing a real `segments` row)
turned out not to map to reality: firmware only uploads bytes to Storage and
never inserts a `segments` row itself - pipeline is the sole writer of that row
via `SupabaseStore.persist_segments`. But firmware's real upload *does* feed
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

`tests/test_firmware_upload_live.py` pins the request-shape details of that
same upload adapter that only the real storage API enforces: a retried upload
of an object already in the bucket succeeds (the next-boot `flush_pending`
retry after a power cut between upload and unlink), a different file under an
existing key is refused rather than overwritten, segments are stored as
`video/mp4`, and the file is streamed rather than read whole into memory.
Same live-instance requirement and skip behavior.

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
  SUPABASE_SERVICE_ROLE_KEY="$SERVICE_ROLE_KEY" SUPABASE_JWT_SECRET="$JWT_SECRET" \
  uv run --project ../integration pytest ../integration
```
