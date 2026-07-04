# Visio Pendant - Supabase Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the Supabase schema, row-level security, and storage buckets that the firmware, cloud pipeline, and mobile app all depend on.

**Architecture:** Postgres migrations under `supabase/migrations/`, verified with pgTAP tests under `supabase/tests/database/` run via the Supabase CLI's local dev stack. Every table is scoped by row-level security so a user only ever sees rows for devices they own.

**Tech Stack:** Supabase CLI, Postgres, pgTAP.

## Global Constraints

- Schema must match the spec's Data Model section exactly: tables `devices`, `device_status`, `segments`, `reels`, `score_weights`.
- `score_weights` defaults: `scene_weight = 0.4`, `audio_weight = 0.3`, `motion_weight = 0.2`.
- `segments.user_feedback` is one of `include`, `exclude`, or `null`.
- Storage buckets: `segments` and `reels`, both private (no public read).
- Every table except `users` (Supabase Auth managed) must have an RLS policy restricting rows to the owning user.

---

### Task 1: Local Supabase project scaffolding

**Files:**
- Create: `supabase/config.toml` (via CLI)
- Create: `supabase/tests/database/.gitkeep`

**Interfaces:**
- Produces: a runnable local Supabase stack (`supabase start`) and a working `supabase test db` command that later tasks add test files to.

- [ ] **Step 1: Initialize the Supabase project**

Run:
```bash
cd /home/viet/git/vision
supabase init
```
Expected: creates `supabase/config.toml`, `supabase/migrations/`, `supabase/seed.sql`.

- [ ] **Step 2: Start the local stack**

Run: `supabase start`
Expected: output ends with a block of local URLs/keys (`API URL`, `DB URL`, `anon key`, `service_role key`). Note the `DB URL` for later manual checks.

- [ ] **Step 3: Create the pgTAP test directory**

```bash
mkdir -p supabase/tests/database
touch supabase/tests/database/.gitkeep
```

- [ ] **Step 4: Verify the test runner works with zero tests**

Run: `supabase test db`
Expected: exits 0 (no test files yet is fine - this just confirms the CLI, pgTAP extension, and local DB are wired together).

- [ ] **Step 5: Commit**

```bash
git add supabase/config.toml supabase/tests/database/.gitkeep
git commit -m "chore: scaffold supabase project"
```

---

### Task 2: `devices` table

**Files:**
- Create: `supabase/tests/database/devices.test.sql`
- Create: `supabase/migrations/20260704120000_create_devices.sql`

**Interfaces:**
- Produces: `public.devices(device_id uuid pk, user_id uuid fk auth.users, name text, created_at timestamptz)` - every later table's `device_id` foreign key points here.

- [ ] **Step 1: Write the failing pgTAP test**

`supabase/tests/database/devices.test.sql`:
```sql
begin;
select plan(5);

select has_table('public', 'devices', 'devices table should exist');
select has_column('public', 'devices', 'device_id', 'devices should have device_id');
select col_is_pk('public', 'devices', 'device_id', 'device_id should be the primary key');
select col_type_is('public', 'devices', 'user_id', 'uuid', 'user_id should be uuid');
select col_not_null('public', 'devices', 'name', 'name should be not null');

select * from finish();
rollback;
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `supabase test db`
Expected: FAIL - `relation "public.devices" does not exist`.

- [ ] **Step 3: Write the migration**

`supabase/migrations/20260704120000_create_devices.sql`:
```sql
create table public.devices (
  device_id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null,
  created_at timestamptz not null default now()
);

alter table public.devices enable row level security;
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `supabase test db`
Expected: PASS (5/5).

- [ ] **Step 5: Commit**

```bash
git add supabase/tests/database/devices.test.sql supabase/migrations/20260704120000_create_devices.sql
git commit -m "feat: add devices table"
```

---

### Task 3: `device_status` table

**Files:**
- Create: `supabase/tests/database/device_status.test.sql`
- Create: `supabase/migrations/20260704120100_create_device_status.sql`

**Interfaces:**
- Consumes: `public.devices(device_id)` from Task 2.
- Produces: `public.device_status(device_id uuid pk/fk, battery_pct int, storage_used_gb numeric, storage_free_gb numeric, segments_pending int, segments_uploaded_today int, recording_active bool, updated_at timestamptz)`.

- [ ] **Step 1: Write the failing pgTAP test**

`supabase/tests/database/device_status.test.sql`:
```sql
begin;
select plan(4);

select has_table('public', 'device_status', 'device_status table should exist');
select col_is_pk('public', 'device_status', 'device_id', 'device_id should be the primary key');
select col_is_fk('public', 'device_status', 'device_id', 'device_id should reference devices');
select col_type_is('public', 'device_status', 'battery_pct', 'integer', 'battery_pct should be integer');

select * from finish();
rollback;
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `supabase test db`
Expected: FAIL - `relation "public.device_status" does not exist`.

- [ ] **Step 3: Write the migration**

`supabase/migrations/20260704120100_create_device_status.sql`:
```sql
create table public.device_status (
  device_id uuid primary key references public.devices(device_id) on delete cascade,
  battery_pct integer not null default 100,
  storage_used_gb numeric not null default 0,
  storage_free_gb numeric not null default 0,
  segments_pending integer not null default 0,
  segments_uploaded_today integer not null default 0,
  recording_active boolean not null default false,
  updated_at timestamptz not null default now()
);

alter table public.device_status enable row level security;
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `supabase test db`
Expected: PASS (4/4).

- [ ] **Step 5: Commit**

```bash
git add supabase/tests/database/device_status.test.sql supabase/migrations/20260704120100_create_device_status.sql
git commit -m "feat: add device_status table"
```

---

### Task 4: `segments` table

**Files:**
- Create: `supabase/tests/database/segments.test.sql`
- Create: `supabase/migrations/20260704120200_create_segments.sql`

**Interfaces:**
- Consumes: `public.devices(device_id)` from Task 2.
- Produces: `public.segments(id uuid pk, device_id uuid fk, recorded_at timestamptz, duration_sec int, s3_key text, motion_score numeric, audio_score numeric, scene_score numeric, composite_score numeric, manually_flagged bool, user_feedback text check)`.

- [ ] **Step 1: Write the failing pgTAP test**

`supabase/tests/database/segments.test.sql`:
```sql
begin;
insert into auth.users (id, email) values ('00000000-0000-0000-0000-000000000001', 'test@example.com');
insert into public.devices (device_id, user_id, name) values ('00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000001', 'test-device');

select plan(4);

select has_table('public', 'segments', 'segments table should exist');
select col_is_fk('public', 'segments', 'device_id', 'device_id should reference devices');

prepare valid_feedback as
  insert into public.segments (device_id, recorded_at, duration_sec, s3_key, user_feedback)
  select device_id, now(), 300, 'x', 'include' from public.devices limit 1;
select lives_ok('valid_feedback', 'user_feedback accepts include');

prepare invalid_feedback as
  insert into public.segments (device_id, recorded_at, duration_sec, s3_key, user_feedback)
  select device_id, now(), 300, 'x', 'maybe' from public.devices limit 1;
select throws_ok('invalid_feedback', '23514', null, 'user_feedback rejects values outside include/exclude');

select * from finish();
rollback;
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `supabase test db`
Expected: FAIL - `relation "public.segments" does not exist`.

- [ ] **Step 3: Write the migration**

`supabase/migrations/20260704120200_create_segments.sql`:
```sql
create table public.segments (
  id uuid primary key default gen_random_uuid(),
  device_id uuid not null references public.devices(device_id) on delete cascade,
  recorded_at timestamptz not null,
  duration_sec integer not null,
  s3_key text not null,
  motion_score numeric,
  audio_score numeric,
  scene_score numeric,
  composite_score numeric,
  manually_flagged boolean not null default false,
  user_feedback text check (user_feedback in ('include', 'exclude'))
);

alter table public.segments enable row level security;
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `supabase test db`
Expected: PASS (4/4).

- [ ] **Step 5: Commit**

```bash
git add supabase/tests/database/segments.test.sql supabase/migrations/20260704120200_create_segments.sql
git commit -m "feat: add segments table"
```

---

### Task 5: `reels` table

**Files:**
- Create: `supabase/tests/database/reels.test.sql`
- Create: `supabase/migrations/20260704120300_create_reels.sql`

**Interfaces:**
- Consumes: `public.devices(device_id)` from Task 2.
- Produces: `public.reels(id uuid pk, device_id uuid fk, date date, s3_key text, duration_sec int, style text, created_at timestamptz)`.

- [ ] **Step 1: Write the failing pgTAP test**

`supabase/tests/database/reels.test.sql`:
```sql
begin;
select plan(3);

select has_table('public', 'reels', 'reels table should exist');
select col_is_fk('public', 'reels', 'device_id', 'device_id should reference devices');
select col_type_is('public', 'reels', 'date', 'date', 'date column should be type date');

select * from finish();
rollback;
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `supabase test db`
Expected: FAIL - `relation "public.reels" does not exist`.

- [ ] **Step 3: Write the migration**

`supabase/migrations/20260704120300_create_reels.sql`:
```sql
create table public.reels (
  id uuid primary key default gen_random_uuid(),
  device_id uuid not null references public.devices(device_id) on delete cascade,
  date date not null,
  s3_key text not null,
  duration_sec integer not null,
  style text not null default 'clean',
  created_at timestamptz not null default now()
);

alter table public.reels enable row level security;
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `supabase test db`
Expected: PASS (3/3).

- [ ] **Step 5: Commit**

```bash
git add supabase/tests/database/reels.test.sql supabase/migrations/20260704120300_create_reels.sql
git commit -m "feat: add reels table"
```

---

### Task 6: `score_weights` table

**Files:**
- Create: `supabase/tests/database/score_weights.test.sql`
- Create: `supabase/migrations/20260704120400_create_score_weights.sql`

**Interfaces:**
- Produces: `public.score_weights(user_id uuid pk/fk auth.users, scene_weight numeric default 0.4, audio_weight numeric default 0.3, motion_weight numeric default 0.2)`.

- [ ] **Step 1: Write the failing pgTAP test**

`supabase/tests/database/score_weights.test.sql`:
```sql
begin;
insert into auth.users (id, email) values ('00000000-0000-0000-0000-000000000003', 'weights@example.com');

select plan(3);

select has_table('public', 'score_weights', 'score_weights table should exist');
select col_is_pk('public', 'score_weights', 'user_id', 'user_id should be the primary key');

insert into public.score_weights (user_id) values ('00000000-0000-0000-0000-000000000003');

select results_eq(
  $$select scene_weight, audio_weight, motion_weight from public.score_weights where user_id = '00000000-0000-0000-0000-000000000003'$$,
  $$values (0.4::numeric, 0.3::numeric, 0.2::numeric)$$,
  'defaults should be 0.4/0.3/0.2'
);

select * from finish();
rollback;
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `supabase test db`
Expected: FAIL - `relation "public.score_weights" does not exist`.

- [ ] **Step 3: Write the migration**

`supabase/migrations/20260704120400_create_score_weights.sql`:
```sql
create table public.score_weights (
  user_id uuid primary key references auth.users(id) on delete cascade,
  scene_weight numeric not null default 0.4,
  audio_weight numeric not null default 0.3,
  motion_weight numeric not null default 0.2
);

alter table public.score_weights enable row level security;
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `supabase test db`
Expected: PASS (3/3).

- [ ] **Step 5: Commit**

```bash
git add supabase/tests/database/score_weights.test.sql supabase/migrations/20260704120400_create_score_weights.sql
git commit -m "feat: add score_weights table"
```

---

### Task 7: Row-level security policies

**Files:**
- Create: `supabase/tests/database/rls_policies.test.sql`
- Create: `supabase/migrations/20260704120500_rls_policies.sql`

**Interfaces:**
- Consumes: all five tables from Tasks 2-6.
- Produces: RLS policies such that `auth.uid()` must match (directly or via the owning device's `user_id`) for any row to be visible.

- [ ] **Step 1: Write the failing pgTAP test**

`supabase/tests/database/rls_policies.test.sql`:
```sql
begin;
insert into auth.users (id, email) values
  ('00000000-0000-0000-0000-0000000000a1', 'owner@example.com'),
  ('00000000-0000-0000-0000-0000000000a2', 'other@example.com');
insert into public.devices (device_id, user_id, name) values
  ('00000000-0000-0000-0000-0000000000b1', '00000000-0000-0000-0000-0000000000a1', 'owner-device');

select plan(2);

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000a2", "role": "authenticated"}';

select is_empty(
  $$select 1 from public.devices where device_id = '00000000-0000-0000-0000-0000000000b1'$$,
  'a different user should not see another user''s device'
);

set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000a1", "role": "authenticated"}';

select isnt_empty(
  $$select 1 from public.devices where device_id = '00000000-0000-0000-0000-0000000000b1'$$,
  'the owning user should see their own device'
);

select * from finish();
rollback;
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `supabase test db`
Expected: FAIL - both selects return empty because RLS is enabled with no policies yet (default-deny), so the "owner should see their own device" assertion fails.

- [ ] **Step 3: Write the migration**

`supabase/migrations/20260704120500_rls_policies.sql`:
```sql
create policy "devices_owner_access" on public.devices
  for all using (auth.uid() = user_id);

create policy "device_status_owner_access" on public.device_status
  for all using (
    auth.uid() = (select user_id from public.devices d where d.device_id = device_status.device_id)
  );

create policy "segments_owner_access" on public.segments
  for all using (
    auth.uid() = (select user_id from public.devices d where d.device_id = segments.device_id)
  );

create policy "reels_owner_access" on public.reels
  for all using (
    auth.uid() = (select user_id from public.devices d where d.device_id = reels.device_id)
  );

create policy "score_weights_owner_access" on public.score_weights
  for all using (auth.uid() = user_id);
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `supabase test db`
Expected: PASS (2/2).

- [ ] **Step 5: Commit**

```bash
git add supabase/tests/database/rls_policies.test.sql supabase/migrations/20260704120500_rls_policies.sql
git commit -m "feat: add row-level security policies"
```

---

### Task 8: Storage buckets

**Files:**
- Create: `supabase/tests/database/storage_buckets.test.sql`
- Create: `supabase/migrations/20260704120600_storage_buckets.sql`

**Interfaces:**
- Produces: private `segments` and `reels` storage buckets with policies restricting access to the owning user (object path convention: `{device_id}/{filename}`, matched against `public.devices.user_id`).

- [ ] **Step 1: Write the failing pgTAP test**

`supabase/tests/database/storage_buckets.test.sql`:
```sql
begin;
select plan(2);

select is(
  (select public from storage.buckets where id = 'segments'),
  false,
  'segments bucket should be private'
);
select is(
  (select public from storage.buckets where id = 'reels'),
  false,
  'reels bucket should be private'
);

select * from finish();
rollback;
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `supabase test db`
Expected: FAIL - `select public from storage.buckets where id = 'segments'` returns no row, so `is()` fails against `null`.

- [ ] **Step 3: Write the migration**

`supabase/migrations/20260704120600_storage_buckets.sql`:
```sql
insert into storage.buckets (id, name, public)
values ('segments', 'segments', false), ('reels', 'reels', false);

create policy "segments_bucket_owner_access" on storage.objects
  for all using (
    bucket_id = 'segments'
    and auth.uid() = (
      select user_id from public.devices d
      where d.device_id::text = (storage.foldername(name))[1]
    )
  );

create policy "reels_bucket_owner_access" on storage.objects
  for all using (
    bucket_id = 'reels'
    and auth.uid() = (
      select user_id from public.devices d
      where d.device_id::text = (storage.foldername(name))[1]
    )
  );
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `supabase test db`
Expected: PASS (2/2).

- [ ] **Step 5: Commit**

```bash
git add supabase/tests/database/storage_buckets.test.sql supabase/migrations/20260704120600_storage_buckets.sql
git commit -m "feat: add segments and reels storage buckets"
```

---

### Task 9: `push_token` column on `devices`

**Files:**
- Create: `supabase/tests/database/devices_push_token.test.sql`
- Create: `supabase/migrations/20260704120700_add_push_token_to_devices.sql`

**Interfaces:**
- Consumes: `public.devices` from Task 2.
- Produces: `public.devices.push_token` (nullable text) - the mobile app writes its Expo push token here after pairing; the cloud pipeline reads it to deliver the nightly notification (see [`2026-07-04-visio-pipeline.md`](2026-07-04-visio-pipeline.md) Task 9's `notify_reel_ready`). Nullable because a device can exist before its companion app has registered a token.

- [ ] **Step 1: Write the failing pgTAP test**

`supabase/tests/database/devices_push_token.test.sql`:
```sql
begin;
select plan(2);

select has_column('public', 'devices', 'push_token', 'devices should have a push_token column');
select col_is_null('public', 'devices', 'push_token', 'push_token should be nullable');

select * from finish();
rollback;
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `supabase test db`
Expected: FAIL - `has_column` returns false, `push_token` does not exist yet.

- [ ] **Step 3: Write the migration**

`supabase/migrations/20260704120700_add_push_token_to_devices.sql`:
```sql
alter table public.devices add column push_token text;
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `supabase test db`
Expected: PASS (2/2).

- [ ] **Step 5: Commit**

```bash
git add supabase/tests/database/devices_push_token.test.sql supabase/migrations/20260704120700_add_push_token_to_devices.sql
git commit -m "feat: add push_token column to devices"
```

---

## Handoff

Once all 8 tasks pass, run the full suite one more time (`supabase test db`) and confirm every file passes, then note the local `API URL`, `anon key`, and `service_role key` from `supabase status` - Epics 1, 2, and 3 need these to configure their Supabase clients.
