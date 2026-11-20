begin;
-- a1 owner, a2 another account, e1 the pendant's device user for b1, e9 an
-- anonymous user that never paired.
insert into auth.users (id, email, is_anonymous) values
  ('00000000-0000-0000-0000-0000000000a1', 'owner@example.com', false),
  ('00000000-0000-0000-0000-0000000000a2', 'other@example.com', false),
  ('00000000-0000-0000-0000-0000000000e1', null, true),
  ('00000000-0000-0000-0000-0000000000e9', null, true);
insert into public.devices (device_id, user_id, name, device_user_id) values
  ('00000000-0000-0000-0000-0000000000b1', '00000000-0000-0000-0000-0000000000a1', 'owner-device', '00000000-0000-0000-0000-0000000000e1'),
  ('00000000-0000-0000-0000-0000000000b2', '00000000-0000-0000-0000-0000000000a1', 'owner-device-2', null);
insert into public.device_status (device_id) values
  ('00000000-0000-0000-0000-0000000000b1');
insert into public.segments (id, device_id, recorded_at, duration_sec, s3_key) values
  ('00000000-0000-0000-0000-0000000000c1', '00000000-0000-0000-0000-0000000000b1', now(), 60, 'b1/seg1.mp4');
insert into public.reels (id, device_id, date, s3_key, duration_sec) values
  ('00000000-0000-0000-0000-0000000000d1', '00000000-0000-0000-0000-0000000000b1', current_date, 'b1/reel1.mp4', 90);
insert into public.score_weights (user_id) values
  ('00000000-0000-0000-0000-0000000000a1');

select plan(43);

-- The pendant's device user is in the authenticated role, so the grants stay;
-- the policies decide which authenticated user they apply to.
select ok(
  has_table_privilege('authenticated', 'public.device_status', 'insert'),
  'authenticated role should have insert privilege on device_status'
);

select ok(
  has_table_privilege('authenticated', 'public.device_status', 'update'),
  'authenticated role should have update privilege on device_status'
);

-- claim_pairing is the only creator of devices rows, and only the pairing
-- functions write the ownership and pairing columns.
select ok(
  not has_table_privilege('authenticated', 'public.devices', 'insert'),
  'authenticated role should have no insert privilege on devices'
);

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000a2", "role": "authenticated"}';

select is_empty(
  $$select 1 from public.devices where device_id = '00000000-0000-0000-0000-0000000000b1'$$,
  'a different user should not see another user''s device'
);

select is_empty(
  $$select 1 from public.device_status where device_id = '00000000-0000-0000-0000-0000000000b1'$$,
  'a different user should not see another user''s device status'
);

select throws_ok(
  $$insert into public.device_status (device_id) values
    ('00000000-0000-0000-0000-0000000000b2')$$,
  '42501',
  null,
  'a different user should not be able to insert device status for another user''s device'
);

select is_empty(
  $$select 1 from public.segments where id = '00000000-0000-0000-0000-0000000000c1'$$,
  'a different user should not see another user''s segments'
);

select is_empty(
  $$select 1 from public.reels where id = '00000000-0000-0000-0000-0000000000d1'$$,
  'a different user should not see another user''s reels'
);

select is_empty(
  $$select 1 from public.score_weights where user_id = '00000000-0000-0000-0000-0000000000a1'$$,
  'a different user should not see another user''s score weights'
);

set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000a1", "role": "authenticated"}';

select isnt_empty(
  $$select 1 from public.devices where device_id = '00000000-0000-0000-0000-0000000000b1'$$,
  'the owning user should see their own device'
);

select isnt_empty(
  $$select 1 from public.device_status where device_id = '00000000-0000-0000-0000-0000000000b1'$$,
  'the owning user should see their own device status'
);

select isnt_empty(
  $$select 1 from public.segments where id = '00000000-0000-0000-0000-0000000000c1'$$,
  'the owning user should see their own segments'
);

select isnt_empty(
  $$select 1 from public.reels where id = '00000000-0000-0000-0000-0000000000d1'$$,
  'the owning user should see their own reels'
);

select isnt_empty(
  $$select 1 from public.score_weights where user_id = '00000000-0000-0000-0000-0000000000a1'$$,
  'the owning user should see their own score weights'
);

select lives_ok(
  $$update public.devices set push_token = 'expo-token'
    where device_id = '00000000-0000-0000-0000-0000000000b1'$$,
  'the owning user should be able to update their own device'
);

select lives_ok(
  $$update public.score_weights set motion_weight = 0.5
    where user_id = '00000000-0000-0000-0000-0000000000a1'$$,
  'the owning user should be able to update their own score weights'
);

select lives_ok(
  $$update public.segments set user_feedback = 'include'
    where id = '00000000-0000-0000-0000-0000000000c1'$$,
  'the owning user should be able to update user_feedback on their own segments'
);

select throws_ok(
  $$update public.segments set composite_score = 0.99
    where id = '00000000-0000-0000-0000-0000000000c1'$$,
  '42501',
  null,
  'the owning user should not be able to update pipeline-owned segment scores'
);

select throws_ok(
  $$insert into public.segments (device_id, recorded_at, duration_sec, s3_key) values
    ('00000000-0000-0000-0000-0000000000b1', now(), 60, 'b1/seg2.mp4')$$,
  '42501',
  null,
  'the owning user should not be able to insert segments directly'
);

select throws_ok(
  $$insert into public.reels (device_id, date, s3_key, duration_sec) values
    ('00000000-0000-0000-0000-0000000000b1', current_date, 'b1/reel2.mp4', 90)$$,
  '42501',
  null,
  'the owning user should not be able to insert reels directly'
);

select throws_ok(
  $$update public.reels set s3_key = 'b1/tampered.mp4'
    where id = '00000000-0000-0000-0000-0000000000d1'$$,
  '42501',
  null,
  'the owning user should not be able to update reels'
);

select lives_ok(
  $$update public.devices set name = 'renamed'
    where device_id = '00000000-0000-0000-0000-0000000000b1'$$,
  'the owning user should be able to rename their own device'
);

select throws_ok(
  $$insert into public.devices (user_id, name) values
    ('00000000-0000-0000-0000-0000000000a1', 'direct')$$,
  '42501',
  null,
  'the owning user should not be able to insert a device directly'
);

select throws_ok(
  $$update public.devices set device_user_id = '00000000-0000-0000-0000-0000000000e9'
    where device_id = '00000000-0000-0000-0000-0000000000b2'$$,
  '42501',
  null,
  'the owning user should not be able to attach a user to their device'
);

select throws_ok(
  $$update public.devices set user_id = '00000000-0000-0000-0000-0000000000a2'
    where device_id = '00000000-0000-0000-0000-0000000000b1'$$,
  '42501',
  null,
  'the owning user should not be able to hand their device to another account'
);

-- The pendant writes device_status under its own login now; the owner only reads.
select throws_ok(
  $$insert into public.device_status (device_id, battery_pct) values
    ('00000000-0000-0000-0000-0000000000b2', 55)$$,
  '42501',
  null,
  'the owning user should not be able to insert device status'
);

select is_empty(
  $$with u as (
      update public.device_status set battery_pct = 1
      where device_id = '00000000-0000-0000-0000-0000000000b1'
      returning 1
    ) select * from u$$,
  'the owning user should not be able to update device status'
);

-- The pendant's device user.

reset role;
delete from public.device_status where device_id = '00000000-0000-0000-0000-0000000000b1';
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000e1", "role": "authenticated", "is_anonymous": true}';

select lives_ok(
  $$insert into public.device_status (device_id, battery_pct) values
    ('00000000-0000-0000-0000-0000000000b1', 55)$$,
  'the device user should be able to insert its paired device''s status'
);

select lives_ok(
  $$update public.device_status set battery_pct = 60
    where device_id = '00000000-0000-0000-0000-0000000000b1'$$,
  'the device user should be able to update its paired device''s status'
);

-- supabase-py's upsert is INSERT ... ON CONFLICT DO UPDATE with return=representation.
select results_eq(
  $$insert into public.device_status (device_id, battery_pct) values
    ('00000000-0000-0000-0000-0000000000b1', 88)
    on conflict (device_id) do update set battery_pct = excluded.battery_pct
    returning battery_pct$$,
  $$values (88)$$,
  'the device user should be able to upsert its paired device''s status'
);

select throws_ok(
  $$insert into public.device_status (device_id, battery_pct) values
    ('00000000-0000-0000-0000-0000000000b2', 55)$$,
  '42501',
  null,
  'the device user should not be able to insert status for another device'
);

select throws_ok(
  $$update public.device_status set device_id = '00000000-0000-0000-0000-0000000000b2'
    where device_id = '00000000-0000-0000-0000-0000000000b1'$$,
  '42501',
  null,
  'the device user should not be able to move its status row to another device'
);

select is_empty(
  $$select 1 from public.devices$$,
  'the device user should see no devices'
);

select is_empty(
  $$select 1 from public.segments$$,
  'the device user should see no segments'
);

select is_empty(
  $$select 1 from public.reels$$,
  'the device user should see no reels'
);

select throws_ok(
  $$insert into public.score_weights (user_id) values
    ('00000000-0000-0000-0000-0000000000e1')$$,
  '42501',
  null,
  'the device user should not be able to create score weights'
);

-- An anonymous user that never paired can do nothing.

set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000e9", "role": "authenticated", "is_anonymous": true}';

select is_empty(
  $$select 1 from public.device_status$$,
  'an unpaired anonymous user should see no device status'
);

select throws_ok(
  $$insert into public.device_status (device_id) values
    ('00000000-0000-0000-0000-0000000000b2')$$,
  '42501',
  null,
  'an unpaired anonymous user should not be able to insert device status'
);

select throws_ok(
  $$insert into public.devices (user_id, name) values
    ('00000000-0000-0000-0000-0000000000e9', 'squatter')$$,
  '42501',
  null,
  'an unpaired anonymous user should not be able to create a device for itself'
);

select throws_ok(
  $$insert into public.score_weights (user_id) values
    ('00000000-0000-0000-0000-0000000000e9')$$,
  '42501',
  null,
  'an unpaired anonymous user should not be able to create score weights'
);

-- Remove device revokes immediately, even with the same (still valid) claims.

set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000a1", "role": "authenticated"}';
select public.unpair_device('00000000-0000-0000-0000-0000000000b1');
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000e1", "role": "authenticated", "is_anonymous": true}';

select throws_ok(
  $$insert into public.device_status (device_id, battery_pct) values
    ('00000000-0000-0000-0000-0000000000b1', 10)
    on conflict (device_id) do update set battery_pct = excluded.battery_pct$$,
  '42501',
  null,
  'an unpaired device user should not be able to upsert status any more'
);

select is_empty(
  $$with u as (
      update public.device_status set battery_pct = 11
      where device_id = '00000000-0000-0000-0000-0000000000b1'
      returning 1
    ) select * from u$$,
  'an unpaired device user should not be able to update status any more'
);

select is_empty(
  $$select 1 from public.device_status$$,
  'an unpaired device user should not see its device''s status any more'
);

select * from finish();
rollback;
