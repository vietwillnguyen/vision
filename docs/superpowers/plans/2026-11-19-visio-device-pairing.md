# Visio Device Pairing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the pendant its own Supabase login through a one-time pairing code, so that the phone refreshing its session or logging out no longer costs the pendant its login, and turn the two `xfail(strict=True)` tests in `integration/tests/test_device_session_live.py` green.

**Architecture:** One migration adds `devices.device_user_id`, a `private` schema (helpers plus the hashed pairing-code table), three `SECURITY DEFINER` RPCs (`create_pairing_code`, `claim_pairing`, `unpair_device`), a daily `pg_cron` purge, and rewrites the RLS and storage policies so the owner reads and the device user writes.
Firmware replaces "session in the QR" with "scan, join Wi-Fi, sign in anonymously, claim".
The app replaces `getSession()` with `rpc('create_pairing_code')`, adds pairing feedback, a first-pairing entry point and "Remove device".
The pipeline does not change.

**Tech stack:** Postgres (Supabase migrations, pgTAP), Python 3.11 + `uv` + pytest (firmware, integration), TypeScript + Expo + jest (app).

**Source spec:** [`docs/superpowers/specs/2026-11-19-visio-device-pairing-design.md`](../specs/2026-11-19-visio-device-pairing-design.md).
Read it first; it holds the full SQL, the permission matrix, the flows and the security reasoning.
This plan references its sections by name instead of repeating them.

## Global constraints

- Branch `fix/device-own-session` (PR #105), worktree `.claude/worktrees/device-session`.
  Part 1 is already on it; do not undo `_session_persister` or the atomic `write_session_credentials`.
- TDD per task: write the failing test, run it and see it fail for the stated reason, implement the minimum, run it green, commit.
- Commands are the ones in `.github/workflows/tests.yml` and `.github/workflows/lint.yml`; do not invent others.
  - Database: `supabase test db` (repo root, needs `supabase start`).
  - Firmware: `cd firmware && uv run --locked --extra dev pytest`, then `ruff check .`, `ruff format --check .`, `mypy` through the same `uv run --locked --extra dev` prefix.
  - Integration: `cd integration && uv run --locked --extra dev pytest`, with `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY` exported from `supabase status -o env` (`API_URL`, `ANON_KEY`, `SERVICE_ROLE_KEY`).
  - App: `cd app && npx tsc --noEmit && npx jest --ci`, `npm run lint`, `npm run format:check`, and `npx expo export --platform web --output-dir /tmp/web-export-smoke`.
  - Generated types: `scripts/gen-db-types.sh`, then `git diff --exit-code -- app/src/generated/database.ts integration/tests/fixtures/public_schema.json` must be clean after committing.
- Never hand-edit `app/src/generated/database.ts` or `integration/tests/fixtures/public_schema.json`.
- No service-role key in firmware or app code.
  Tests may use it for setup and teardown only.
- Firmware tests use the `Protocol` plus fakes convention (`firmware/tests/fakes.py`); no mocks.
- A changed `supabase/config.toml` only takes effect after `supabase stop && supabase start`.
- Any edit to a file under `docs/superpowers/specs/` needs its `source-sha256` line in `docs/ARCHITECTURE.html` updated in the same commit (`docs/architecture-regeneration.md`).
- Commits: conventional, lowercase imperative subject, body says why, no co-author or tool trailers.
- Never use the em dash character anywhere.

## File map

| File | Change |
|---|---|
| `supabase/config.toml` | enable anonymous sign-ins; raise the local anonymous rate limit |
| `supabase/migrations/20261119120000_device_pairing.sql` | new: everything in the spec's "Data model", "RLS and storage policy changes" and "Pairing functions" |
| `supabase/tests/database/device_pairing.test.sql` | new pgTAP suite |
| `supabase/tests/database/rls_policies.test.sql`, `storage_buckets.test.sql`, `devices.test.sql` | updated for the new matrix |
| `scripts/gen-db-types.sh` | add a `functions` section to the schema fixture |
| `firmware/visio_recorder/wifi_onboard.py` | v2 payload |
| `firmware/visio_recorder/pairing.py` | new: state, pending file, `complete_pairing` |
| `firmware/visio_recorder/device_identity.py` | paired identity read and write; registration removed |
| `firmware/visio_recorder/supabase_clients.py` | registration adapter removed; revoked-login error; 409 handling |
| `firmware/visio_recorder/onboarding.py`, `daemon.py`, `upload_limit.py` / `recording_loop.py` | boot state machine; 409 to `rejected/` |
| `integration/tests/conftest.py` and the three live suites | `paired_device` fixture; xfails removed; new live tests |
| `app/src/containers/ReonboardContainer.tsx`, `screens/ReonboardScreen.tsx`, `hooks/useDevice.ts`, `containers/DeviceContainer.tsx`, `screens/DeviceScreen.tsx`, `App.tsx` | pairing code QR, paired and expired steps, first pairing, Remove device |
| `docs/...`, `README.md` | as-built notes |

---

### Task 1: Enable anonymous sign-ins on the local stack

**Files:** Modify `supabase/config.toml`.

- [ ] **Step 1:** Set `enable_anonymous_sign_ins = true`.
  Add a comment above it: the pendant signs in anonymously to get its own login, see the pairing spec; the hosted project needs the same switch turned on by hand.
- [ ] **Step 2:** Under `[auth.rate_limit]` set `anonymous_users = 10000` with a comment: local and CI only, because every pairing in the integration suite is an anonymous sign-in from one IP; the hosted project keeps the default 30.
- [ ] **Step 3:** `supabase stop && supabase start -x imgproxy`, then prove it:
  `curl -s -X POST "$API_URL/auth/v1/signup" -H "apikey: $ANON_KEY" -H "content-type: application/json" -d '{}'` returns a session whose `user.is_anonymous` is `true`.
- [ ] **Step 4:** Run `supabase test db` and the integration suite; both stay green (nothing depends on the switch yet).
- [ ] **Step 5:** Commit `chore(supabase): enable anonymous sign-ins on the local stack`.

### Task 2: Schema - `device_user_id`, `private` schema, pairing-code table, helpers

**Files:** Create `supabase/migrations/20261119120000_device_pairing.sql`; create `supabase/tests/database/device_pairing.test.sql`.

**Tests first** (pgTAP, wrapped in `begin; ... rollback;` like the existing suites):

- [ ] `has_column('public', 'devices', 'device_user_id', ...)`, and `col_is_unique`.
- [ ] Deleting the referenced `auth.users` row sets `device_user_id` to null and keeps the device row.
- [ ] `has_table('private', 'device_pairing_codes', ...)`; RLS is enabled on it; `authenticated` and `anon` have no `select` on it (`has_table_privilege` is false).
- [ ] `has_function('private', 'owned_device_ids')`, `has_function('private', 'paired_device_id')`, `has_function('private', 'is_anonymous')`.
- [ ] With `set local role authenticated` and `request.jwt.claims` for an owner: `private.owned_device_ids()` returns exactly that owner's devices.
- [ ] For a linked device user: `private.paired_device_id()` returns its device; for an unlinked anonymous user it returns null.

Seed pattern: copy the `insert into auth.users (id, email) ...` style from `rls_policies.test.sql`, adding `is_anonymous` (`true` for device users, with a null email).
Claims pattern: `set local request.jwt.claims = '{"sub": "...", "role": "authenticated", "is_anonymous": true}'`.

- [ ] **Run:** `supabase test db` fails on the new file (column and schema do not exist).
- [ ] **Implement:** the spec's "`devices.device_user_id`", "`private` schema", "`private.device_pairing_codes`" and "Helper functions" SQL, verbatim.
  Do not touch policies yet.
- [ ] **Run:** `supabase db reset` (or `supabase migration up --local`), then `supabase test db` green.
- [ ] **Commit:** `feat(supabase): add device user link and pairing code storage`.

### Task 3: `create_pairing_code` and `claim_pairing`

**Files:** Modify the same migration (it has not shipped; keep one migration file) and `device_pairing.test.sql`.

**Tests first:**

- [ ] An owner calling `create_pairing_code('<own device>')` gets one row; `length(code) = 43`; `expires_at` is within 9 to 11 minutes from `now()`.
- [ ] The stored row holds `extensions.digest(code, 'sha256')` and not the code.
- [ ] A second call deletes the first unclaimed code (one live code per owner).
- [ ] `create_pairing_code` throws `42501` for: a device owned by another account; a caller whose `auth.users.is_anonymous` is true; a null `auth.uid()`.
- [ ] `claim_pairing` by a linked-to-nothing anonymous user returns the code's `device_id` and sets `devices.device_user_id` to the caller.
- [ ] The same caller claiming again gets the same `device_id` (idempotent).
- [ ] A different anonymous user claiming the used code throws `P0001` with message `invalid pairing code`.
- [ ] An expired code (update `expires_at` to the past as postgres) and an unknown code throw the same error.
- [ ] A non-anonymous caller throws `42501`.
- [ ] A code with a null `device_id` creates a new `devices` row owned by the code's creator, named `visio-pendant`, linked to the caller.
- [ ] Re-pair: with device user e1 linked, a new code claimed by e2 links e2 and deletes e1 from `auth.users`.
- [ ] A device user that is already linked cannot claim a second code.
- [ ] `anon` cannot execute either function (`has_function_privilege('anon', ...)` is false).

To pass the plaintext code between roles inside one test file, store it in a temp table granted to `authenticated` (the prototype used `create temp table t (code text); grant all on t to authenticated;`).

- [ ] **Run:** red. **Implement:** the two functions and their `revoke`/`grant` from the spec. **Run:** green.
- [ ] **Commit:** `feat(supabase): add pairing code creation and claim functions`.

### Task 4: `unpair_device` and the purge job

**Tests first:**

- [ ] The owner calling `unpair_device` clears `device_user_id` and deletes the anonymous device user.
- [ ] If the linked user has `is_anonymous = false`, the link is cleared and the user is **not** deleted.
- [ ] Another account calling it throws `42501` and changes nothing.
- [ ] It deletes the device's unclaimed pairing codes.
- [ ] `private.purge_stale_pairing_state()` (called as postgres) deletes an unlinked anonymous user older than a day and a code expired more than a day ago, and keeps: a linked device user of any age, an unlinked anonymous user younger than a day, every non-anonymous user.
- [ ] `select jobname from cron.job` contains `purge-stale-pairing-state`.
- [ ] `authenticated` cannot execute `private.purge_stale_pairing_state()`.

- [ ] **Run:** red. **Implement** from the spec's "`unpair_device`" and "Housekeeping". **Run:** green.
- [ ] If `supabase test db` cannot see `cron` (extension missing in the CI image), stop and report; do not silently drop the job.
- [ ] **Commit:** `feat(supabase): add device unpairing and stale pairing cleanup`.

### Task 5: RLS and storage policies for the device user

**Files:** Modify the migration; modify `rls_policies.test.sql`, `storage_buckets.test.sql`, `devices.test.sql` if it asserts owner inserts; extend `device_pairing.test.sql`.

**Tests first** - one assertion per cell of the spec's "Who can do what" table:

- [ ] Device user: `lives_ok` insert and update of its paired device's `device_status`; `INSERT ... ON CONFLICT (device_id) DO UPDATE ... RETURNING` works; insert for another device throws `42501`.
- [ ] Device user: sees zero rows in `devices`, `segments`, `reels`; `score_weights` insert throws `42501`.
- [ ] Device user: `insert into storage.objects (bucket_id, name, owner_id)` under `segments/<paired device>/` with its own `owner_id` lives; under another device's folder throws; into `reels` throws; with a spoofed `owner_id` throws.
- [ ] Device user: sees its own object; does not see an object in the same folder whose `owner_id` is another user.
- [ ] Unpaired anonymous user: every one of the above throws or returns nothing.
- [ ] Owner: selects `devices`, `device_status`, `segments`, `reels`, and objects in both buckets for its devices; `update devices set name`/`push_token` lives.
- [ ] Owner: `insert into devices` throws `42501`; `update devices set device_user_id` and `set user_id` throw `42501`; `insert`/`update` on `device_status` is refused (insert throws, update affects zero rows); insert into either bucket throws.
- [ ] Other account: unchanged refusals.
- [ ] After `unpair_device`, the former device user (same claims) can no longer insert status or objects.
- [ ] Update the existing assertions that say the owner can insert `device_status` and upload to both buckets: they now assert refusal, and the comment "the firmware daemon runs under the owner's user session" is replaced.

- [ ] **Run:** red. **Implement** the spec's policy SQL. **Run:** `supabase db reset && supabase test db` green.
- [ ] **Commit:** `feat(supabase): let the pendant's own user write its device and nothing else`.

### Task 6: Regenerate types and add RPC signatures to the Python fixture

**Files:** Modify `scripts/gen-db-types.sh`; regenerate `app/src/generated/database.ts` and `integration/tests/fixtures/public_schema.json`; modify `integration/tests/test_row_schema_contract.py`.

- [ ] **Test first:** in `test_row_schema_contract.py`, assert the fixture has `functions.claim_pairing.args == {"p_code": "text"}`, `functions.create_pairing_code.args == {"p_device_id": "uuid"}` and `functions.unpair_device.args == {"p_device_id": "uuid"}`.
  Run it: red (no `functions` key).
- [ ] **Implement:** extend the fixture query with a `functions` object built from `information_schema.routines` joined to `information_schema.parameters` (`specific_schema = 'public'`, `parameter_mode = 'IN'`), keyed by routine name, with `args` (name to `data_type`) and `returns`.
  Keep `tables` byte-identical apart from the new `devices.device_user_id` column.
- [ ] **Run:** `scripts/gen-db-types.sh`; confirm `database.ts` now has `device_user_id` and a `Functions` block with the three RPCs; the contract test is green; `cd app && npx tsc --noEmit` is green.
- [ ] **Commit:** `chore: regenerate database types with pairing functions`.

### Task 7: Firmware - QR payload v2

**Files:** Modify `firmware/visio_recorder/wifi_onboard.py`, `firmware/tests/test_wifi_onboard.py`.

**Tests first:**

- [ ] `parse_onboarding_qr_payload('{"v":2,"ssid":"home","password":"password1","pairing_code":"abc"}')` returns `OnboardingPayload(ssid, password, pairing_code)`.
- [ ] A v1 payload (the old four fields, no `v`) raises `QrDecodeError` whose message contains `older app version`.
- [ ] `v` of 1, 3, `"2"` (string) or missing-with-`pairing_code` raises `QrDecodeError` (`unsupported QR version` for a wrong number).
- [ ] Missing, empty or non-string `pairing_code` raises `QrDecodeError`.
- [ ] Error messages never contain the password or the code (assert on a distinctive value).
- [ ] Replace the old `VALID_PAYLOAD` and its dependants.

- [ ] **Run:** `cd firmware && uv run --locked --extra dev pytest tests/test_wifi_onboard.py` red, implement, green.
- [ ] `OnboardingPayload` loses `user_access_token` and `user_refresh_token`; this breaks `onboarding.py` until Task 9, so in this task make `run_onboarding` compile by taking a `pending_path` and calling a `write_pending_pairing` stub you add in Task 8, or do Tasks 7 to 9 as one commit series before running the whole suite.
- [ ] **Commit:** `feat(firmware): parse the pairing-code qr payload`.

### Task 8: Firmware - pairing state and paired identity

**Files:** Create `firmware/visio_recorder/pairing.py`, `firmware/tests/test_pairing.py`; modify `device_identity.py`, `tests/test_device_identity.py`.

**Interfaces to produce:**

```python
class PairingState(Enum): UNPAIRED, PENDING, PAIRED

def pairing_state(data_dir: Path) -> PairingState
def write_pending_pairing(path: Path, pairing_code: str) -> None   # atomic, 0600
def read_pending_pairing(path: Path) -> str | None
def clear_local_pairing(data_dir: Path) -> None   # deletes session.json and pairing.json, drops the marker

# device_identity.py
def load_paired_device_id(state_path: Path) -> str | None      # only when "pairing" == 2
def store_paired_device_id(state_path: Path, device_id: str) -> None   # atomic, fsync
```

**Tests first:**

- [ ] The state table from the spec's "On-disk state", one test per row, including: legacy `device.json` (`{"device_id": ...}` only) plus a `session.json` is `UNPAIRED`; a corrupt `device.json` is `UNPAIRED`; `pairing.json` present wins over everything.
- [ ] `write_pending_pairing` is mode 0600 and leaves no temp file; `read_pending_pairing` of a missing or corrupt file is `None`.
- [ ] `store_paired_device_id` then `load_paired_device_id` round-trips; the file is `{"device_id": ..., "pairing": 2}`.
- [ ] `clear_local_pairing` removes `session.json` and `pairing.json`, and afterwards `pairing_state` is `UNPAIRED`; it keeps the queue directory untouched.
- [ ] Delete the tests for `load_or_create_device_id` and `register_device` together with the functions.

- [ ] Reuse the atomic-write helper: extract the temp-file, fsync, rename, directory-fsync body of `write_session_credentials` into a private `_write_private_text_atomic(path, content)` in `wifi_onboard.py` and call it from all three writers, keeping part 1's tests green.
- [ ] **Run:** red, implement, green. **Commit:** `feat(firmware): track pairing state on disk`.

### Task 9: Firmware - `complete_pairing`

**Files:** Modify `pairing.py`, `supabase_clients.py`, `tests/test_pairing.py`, `tests/test_supabase_clients.py`.

**Interface:**

```python
CLAIM_RPC = "claim_pairing"
CLAIM_ARG = "p_code"

class PairingRejectedError(Exception): ...      # definite: scan again
class PairingUnavailableError(Exception): ...   # retry on next boot

def complete_pairing(
    url: str, key: str, data_dir: Path,
    client_factory: Callable[[str, str], Any] = create_client,
) -> str   # the device_id
```

**Tests first** (a fake client recording calls, in the style of `test_supabase_clients.py`):

- [ ] With no `session.json`: the persister is registered **before** `sign_in_anonymously`, the fake emits `SIGNED_IN`, and `session.json` holds the anonymous session.
- [ ] Then `rpc("claim_pairing", {"p_code": <code>})` is called, `device.json` is written with the returned id and the marker, and `pairing.json` is deleted, in that order.
- [ ] With a `session.json` already present: `set_session` is used and `sign_in_anonymously` is **not** called.
- [ ] `device.json` already written but `pairing.json` still present (crash between the two writes): it claims again and finishes cleanly.
- [ ] A 4xx `APIError` from the RPC raises `PairingRejectedError`, and `pairing.json` and `session.json` are gone.
- [ ] A network error, a 5xx or a 429 from sign-in or the RPC raises `PairingUnavailableError` and leaves every file in place.
- [ ] A non-429 4xx from `sign_in_anonymously` raises `PairingUnavailableError` and logs a message containing `anonymous sign-ins`.
- [ ] `test_row_schema_contract.py` (integration): `CLAIM_RPC` and `CLAIM_ARG` match the fixture's `functions.claim_pairing`.
- [ ] `SupabaseRegistration`, `SupabaseClients.registration` and their test are removed; `build_supabase_clients` no longer reads `auth_response.user.id`.
- [ ] New: `build_supabase_clients` raises `SessionRevokedError` when `set_session` raises `AuthApiError` with a 4xx status other than 429, and re-raises anything else unchanged.

- [ ] **Run:** red, implement, green (firmware and the integration contract test). **Commit:** `feat(firmware): pair with an anonymous login and a one-time code`.

### Task 10: Firmware - boot sequence

**Files:** Modify `onboarding.py`, `daemon.py`, `tests/test_onboarding.py`, `tests/test_daemon.py`.

Extract the pairing gate out of `main()` into a testable function, since `main()` itself is deliberately untested:

```python
def ensure_paired(
    data_dir: Path, scanner: QrScanner, activator: ConnectionActivator,
    led_driver: LedDriver, pair: Callable[[], str],
) -> str | None   # device_id, or None when the daemon must exit 1
```

**Tests first:**

- [ ] `run_onboarding` writes the keyfile and `pairing.json` (not `session.json`) on the first good scan; a v1 scan counts as a failed attempt and scanning continues.
- [ ] `UNPAIRED`: a stale `session.json` is deleted before scanning; after a good scan the activator runs, then `pair`, and the device id is returned.
- [ ] `UNPAIRED` with no successful scan returns `None` and never calls the activator or `pair` (no network use before Wi-Fi activation).
- [ ] `PENDING`: no scan; activator then `pair`.
- [ ] `PENDING`, activation fails: Critical LED, `None`, `pairing.json` kept.
- [ ] `pair` raises `PairingRejectedError`: Critical LED, `None`, next `pairing_state` is `UNPAIRED`.
- [ ] `pair` raises `PairingUnavailableError`: `None`, next `pairing_state` is still `PENDING`.
- [ ] `PAIRED`: no scan, no `pair`; `reactivate_connection_if_configured` runs; the stored device id is returned.
- [ ] A `SessionRevokedError` from `build_supabase_clients` is handled by a small `handle_revoked_login(data_dir, led_driver)` that calls `clear_local_pairing` and applies the Critical LED; test that afterwards the state is `UNPAIRED`.

- [ ] **Implement:** `main()` calls `ensure_paired`, returns 1 on `None`, wraps `build_supabase_clients` to call `handle_revoked_login` and return 1, and drops the `device_registered` marker and `register_device` call.
- [ ] **Run:** the whole firmware suite, `ruff check .`, `ruff format --check .`, `mypy`. **Commit:** `feat(firmware): boot through pairing instead of a borrowed session`.

### Task 11: Firmware - a duplicate the pendant cannot see goes to `rejected/`

**Files:** Modify `uploader.py` (new `ObjectAlreadyStoredError`), `supabase_clients.py`, `upload_limit.py` and `recording_loop.py` where `ObjectTooLargeError` is handled; their tests.

**Tests first:**

- [ ] `SupabaseStorage.upload`: `info` says not found, `upload` raises `StorageApiError` with status 409; the adapter raises `ObjectAlreadyStoredError`.
- [ ] The queue-processing path that moves an unsplittable file to `rejected/` does the same for `ObjectAlreadyStoredError`: the file leaves the queue, lands in `rejected/`, is counted by `segments_rejected`, and is logged at error level.
- [ ] A visible duplicate of the same size still returns quietly, and of a different size still raises `FileExistsError` (existing tests stay green).

- [ ] **Run:** red, implement, green. **Commit:** `fix(firmware): set aside a segment an earlier pairing already stored`.

### Task 12: Integration - pairing fixture and the two pinned tests

**Files:** Modify `integration/tests/conftest.py`, `test_device_session_live.py`.

- [ ] **Step 1:** Add to `conftest.py` a `pair_pendant(env, owner_client, data_dir, device_id=None) -> str` helper that does the real flow: `owner_client.rpc("create_pairing_code", {"p_device_id": device_id})`, builds the v2 JSON, runs it through `parse_onboarding_qr_payload`, `write_pending_pairing`, then `complete_pairing`.
  Add a `paired_device` fixture (owner created with the admin API, device row inserted with the admin client, pendant paired, teardown removes stored objects and deletes the owner, which cascades).
- [ ] **Step 2:** Rewrite `_onboard` in `test_device_session_live.py` to call `pair_pendant`.
  Do **not** remove the `xfail` marks yet.
  Run the file: the two xfail tests now report `XPASS(strict)` failures.
  That is the proof they pass for the right reason.
- [ ] **Step 3:** Remove both `xfail` marks.
  Run: all three session tests green.
- [ ] **Step 4:** Add the new live tests listed in the spec's "Testing strategy" (upload and status as the pendant and owner reads; pendant refused elsewhere; Remove device; re-pair replaces; a used code; unpaired anonymous user).
  For "Remove device": assert `build_supabase_clients` raises `SessionRevokedError` on the next boot, and that the pendant's pre-revocation client is refused by a storage upload.
  Code expiry cannot be aged through the public API, so it is covered by pgTAP only (Task 3); say so in a comment instead of adding a ten-minute sleep.
- [ ] **Step 5:** This is where the spec's open question 3 is settled.
  If the pendant's first real upload is refused by the `owner_id` clause, remove that clause from `segments_bucket_device_insert` only, update the pgTAP spoof assertion, and note it in the spec.
- [ ] **Run:** `cd integration && uv run --locked --extra dev pytest`. **Commit:** `test(integration): pair the pendant with its own login and unpin the session tests`.

### Task 13: Integration - move the upload suites off the owner's session

**Files:** Modify `test_firmware_upload_live.py`, `test_storage_ingestion_live.py`, `test_rls_end_to_end.py` only if it writes as the owner.

- [ ] Replace each "sign in as the owner, then upload" with the `paired_device` fixture's pendant client.
  The owner client stays for the read-side assertions; the admin client stays for the pipeline side.
- [ ] Run the suite twice in a row to prove the raised anonymous rate limit holds.
- [ ] **Commit:** `test(integration): upload as the paired pendant`.

### Task 14: App - `useDevice` exposes pairing and can refresh

**Files:** Modify `app/src/hooks/useDevice.ts`, `app/__tests__/hooks/useDevice.test.tsx`.

- [ ] **Tests first:** the query selects `device_id, name, device_user_id`; `ready` state carries `paired: true` when `device_user_id` is set and `false` when null; calling the returned `refresh()` re-queries and moves `none` to `ready` when a row has appeared.
- [ ] Implement; keep the existing return shape working for current callers (`App.tsx` destructures the state).
- [ ] **Run:** `npx tsc --noEmit && npx jest --ci __tests__/hooks/useDevice.test.tsx`. **Commit:** `feat(app): expose whether the pendant is paired`.

### Task 15: App - pairing-code QR

**Files:** Modify `ReonboardContainer.tsx`, `ReonboardScreen.tsx`, `navigation/DeviceStack.tsx` (pass `deviceId`), and their tests.

- [ ] **Tests first (container):**
  - submitting calls `client.rpc('create_pairing_code', { p_device_id: <deviceId> })` and never `auth.getSession`;
  - the QR value parses to exactly `{ v: 2, ssid, password, pairing_code }`, with no token fields;
  - an RPC `{ error }` and a rejected promise both reach the `error` step;
  - with jest fake timers, while `ready` it polls `devices`; a changed `device_user_id` moves to `paired`; the poll stops on unmount and on `paired`;
  - when `expires_at` passes it moves to `expired`, and "Generate a new code" calls the RPC again.
- [ ] **Tests first (screen):** `paired` and `expired` render their text and buttons; the privacy hint is the new wording; the QR no longer passes `ecl="L"`.
- [ ] Add one shared constant for the payload version and a comment pointing at the spec's "QR payload v2" as the contract.
- [ ] **Run:** `npx tsc --noEmit && npx jest --ci`, `npm run lint`, `npm run format:check`. **Commit:** `feat(app): put a one-time pairing code in the qr instead of the login`.

### Task 16: App - first pairing and Remove device

**Files:** Modify `App.tsx`, `DeviceContainer.tsx`, `DeviceScreen.tsx`, and `__tests__/App.test.tsx`, `containers/DeviceContainer.test.tsx`, `screens/DeviceScreen.test.tsx`.

- [ ] **Tests first:**
  - with no device, the app shows "Set up your Visio" and a button that opens the pairing screen with no device id; after `paired`, "Done" calls `refresh()` and the tabs render;
  - the Device screen shows "Remove device"; pressing it asks for confirmation; confirming calls `client.rpc('unpair_device', { p_device_id })`; cancelling calls nothing; an RPC error is shown;
  - when `paired` is false the Device screen shows "Not paired" and a "Pair pendant" button that navigates to the pairing screen.
- [ ] Implement; then run the app in a browser (`npx expo start --web` against the local stack) and look at every new state: setup, form, QR, paired, expired, error, Device tab paired and not paired, the confirmation dialog.
  Fix anything that looks off, and commit screenshots if the repo's PR convention asks for them.
- [ ] **Run:** `npx tsc --noEmit && npx jest --ci`, lint, format check, and the web export smoke check. **Commit:** `feat(app): set up a first pendant and remove a paired one`.

### Task 17: Docs and as-built notes

**Files:** `docs/superpowers/specs/2026-11-19-visio-device-pairing-design.md` (status to implemented, open questions resolved), `2026-07-20-wifi-reonboard-qr-screen-design.md`, `2026-07-04-visio-pendant-design.md` (WiFi Onboarding and Tab 3 as-built notes, startup step 3), `docs/ARCHITECTURE.html`, `README.md` (firmware and Supabase sections), `integration/README.md`, `AGENTS.md` only if a new sharp edge earned it.

- [ ] Regenerate `docs/ARCHITECTURE.html` per `docs/architecture-regeneration.md`: move the pairing section from planned to as-built, update the data model table, the `device_status` write-access note, the Device card and the startup diagram, and rewrite every changed spec's manifest line.
- [ ] Verify: `sed -n 's|^<!-- source-sha256: \(.*\) -->$|\1|p' docs/ARCHITECTURE.html | sha256sum -c -`.
- [ ] **Commit:** `docs: record device pairing as built`.

### Task 18: Full verification

- [ ] `supabase db reset && supabase test db`.
- [ ] `scripts/gen-db-types.sh && git diff --exit-code -- app/src/generated/database.ts integration/tests/fixtures/public_schema.json`.
- [ ] Firmware, pipeline and integration: `uv run --locked --extra dev pytest`, `ruff check .`, `ruff format --check .`, `mypy` in each.
- [ ] App: `npx tsc --noEmit`, `npx jest --ci`, `npm run lint`, `npm run format:check`, web export.
- [ ] `scripts/hooks/tests/test-pre-commit.sh`.
- [ ] `grep -rn "user_access_token\|user_refresh_token\|device_registered\|SupabaseRegistration" app/src firmware/visio_recorder integration/tests` returns nothing.
- [ ] Update the PR #105 description: part 2 included, the manual hosted steps from the spec's "Rollout", and the one-time re-onboarding.

## Manual steps for the owner (not automatable)

Listed in the spec's "Rollout and manual steps": enable anonymous sign-ins on the hosted project, keep CAPTCHA off and session timeouts unset, apply the migration, ship firmware and app together, re-onboard the pendant once, verify.
