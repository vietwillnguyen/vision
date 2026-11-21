-- Read-only accounts: the public demo signs in as a user whose
-- auth.users.raw_app_meta_data carries "read_only": true. That user can read
-- its own (demo) device's rows and storage objects through the ordinary owner
-- policies, and every write is refused by the database itself - restrictive
-- RLS policies on every public table and on storage.objects, plus triggers on
-- auth.users and auth.mfa_factors so the account cannot change its own login.
--
-- Each denial is paired with the same statement succeeding for an ordinary
-- owner, so a test can only pass because the account is read-only, never
-- because the statement was invalid or the role lacked the grant anyway.
begin;
insert into auth.users (id, email, raw_app_meta_data, encrypted_password) values
  ('00000000-0000-0000-0000-0000000000e1', 'demo@example.com',
   '{"provider": "email", "read_only": true}', 'demo-hash'),
  ('00000000-0000-0000-0000-0000000000e2', 'owner@example.com',
   '{"provider": "email"}', 'owner-hash');
insert into public.devices (device_id, user_id, name) values
  ('00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-0000000000e1', 'demo-device'),
  ('00000000-0000-0000-0000-0000000000f2', '00000000-0000-0000-0000-0000000000e2', 'owner-device');
insert into public.device_status (device_id) values
  ('00000000-0000-0000-0000-0000000000f1'),
  ('00000000-0000-0000-0000-0000000000f2');
insert into public.segments (id, device_id, recorded_at, duration_sec, s3_key) values
  ('00000000-0000-0000-0000-0000000000c1', '00000000-0000-0000-0000-0000000000f1', now(), 60,
   '00000000-0000-0000-0000-0000000000f1/20261120_090000.mp4'),
  ('00000000-0000-0000-0000-0000000000c2', '00000000-0000-0000-0000-0000000000f2', now(), 60,
   '00000000-0000-0000-0000-0000000000f2/20261120_090000.mp4');
insert into public.reels (id, device_id, date, s3_key, duration_sec) values
  ('00000000-0000-0000-0000-0000000000d1', '00000000-0000-0000-0000-0000000000f1', current_date,
   '00000000-0000-0000-0000-0000000000f1/20261120.mp4', 90);
insert into public.score_weights (user_id) values
  ('00000000-0000-0000-0000-0000000000e1'),
  ('00000000-0000-0000-0000-0000000000e2');
insert into storage.objects (bucket_id, name) values
  ('segments', '00000000-0000-0000-0000-0000000000f1/20261120_090000.mp4'),
  ('reels', '00000000-0000-0000-0000-0000000000f1/20261120.mp4'),
  ('segments', '00000000-0000-0000-0000-0000000000f2/20261120_090000.mp4');

select plan(47);

-- Guard: every public table, and storage.objects, carries a restrictive
-- read-only policy for each write command. A table added later without one
-- fails here instead of silently giving the demo account a writable surface.
select is_empty(
  $$select c.relname, cmd
      from pg_class c
      cross join (values ('INSERT'), ('UPDATE'), ('DELETE')) as cmds(cmd)
     where c.relnamespace = 'public'::regnamespace
       and c.relkind = 'r'
       and not exists (
         select 1 from pg_policies p
          where p.schemaname = 'public'
            and p.tablename = c.relname
            and p.permissive = 'RESTRICTIVE'
            and p.cmd = cmds.cmd
            and p.policyname like '%_read_only_%'
       )$$,
  'every public table should have a restrictive read-only policy for insert, update and delete'
);
select is(
  (select count(*)::int from pg_policies
    where schemaname = 'storage' and tablename = 'objects'
      and permissive = 'RESTRICTIVE' and policyname like '%_read_only_%'
      and cmd in ('INSERT', 'UPDATE', 'DELETE')),
  3,
  'storage.objects should have a restrictive read-only policy for insert, update and delete'
);
select ok(
  not has_function_privilege('anon', 'private.is_read_only_user()', 'execute'),
  'anon should not be able to call private.is_read_only_user()'
);

-- Storage refuses a bare SQL delete on storage.objects unless the request
-- comes through the Storage API, which sets this; set it here so the deletes
-- below reach the RLS policies under test, exactly as an API call would.
set local storage.allow_delete_query = 'true';

set local role authenticated;

-- The demo account reads its own device's data.
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000e1", "role": "authenticated"}';

select is(private.is_read_only_user(), true, 'the demo account should be read-only');
select isnt_empty(
  $$select 1 from public.devices where device_id = '00000000-0000-0000-0000-0000000000f1'$$,
  'the demo account should see the demo device'
);
select isnt_empty(
  $$select 1 from public.device_status where device_id = '00000000-0000-0000-0000-0000000000f1'$$,
  'the demo account should see the demo device status'
);
select isnt_empty(
  $$select 1 from public.segments where id = '00000000-0000-0000-0000-0000000000c1'$$,
  'the demo account should see the demo segments'
);
select isnt_empty(
  $$select 1 from public.reels where id = '00000000-0000-0000-0000-0000000000d1'$$,
  'the demo account should see the demo reel'
);
select isnt_empty(
  $$select 1 from public.score_weights where user_id = '00000000-0000-0000-0000-0000000000e1'$$,
  'the demo account should see its score weights'
);
select is(
  (select count(*)::int from storage.objects
    where name like '00000000-0000-0000-0000-0000000000f1/%'),
  2,
  'the demo account should see the demo device''s segment and reel objects'
);
select is_empty(
  $$select 1 from public.segments where device_id = '00000000-0000-0000-0000-0000000000f2'
    union all
    select 1 from storage.objects where name like '00000000-0000-0000-0000-0000000000f2/%'$$,
  'the demo account should not see another account''s segments or objects'
);

-- Every write is refused. Inserts fail the WITH CHECK; updates and deletes
-- find no row they may touch, so they change nothing.
select throws_ok(
  $$insert into public.devices (user_id, name)
    values ('00000000-0000-0000-0000-0000000000e1', 'second demo device')$$,
  '42501', null, 'the demo account should not be able to add a device'
);
select is_empty(
  $$update public.devices set name = 'renamed'
    where device_id = '00000000-0000-0000-0000-0000000000f1' returning 1$$,
  'the demo account should not be able to rename the demo device'
);
select is_empty(
  $$update public.devices set push_token = 'ExponentPushToken[x]'
    where device_id = '00000000-0000-0000-0000-0000000000f1' returning 1$$,
  'the demo account should not be able to register a push token'
);
select is_empty(
  $$delete from public.devices
    where device_id = '00000000-0000-0000-0000-0000000000f1' returning 1$$,
  'the demo account should not be able to delete the demo device'
);
select throws_ok(
  $$insert into public.device_status (device_id) values ('00000000-0000-0000-0000-0000000000f1')
    on conflict (device_id) do update set battery_pct = 1$$,
  '42501', null, 'the demo account should not be able to upsert the device status'
);
select is_empty(
  $$update public.device_status set battery_pct = 1
    where device_id = '00000000-0000-0000-0000-0000000000f1' returning 1$$,
  'the demo account should not be able to update the device status'
);
select is_empty(
  $$update public.segments set user_feedback = 'include'
    where id = '00000000-0000-0000-0000-0000000000c1' returning 1$$,
  'the demo account should not be able to set segment feedback'
);
select throws_ok(
  $$insert into public.score_weights (user_id) values ('00000000-0000-0000-0000-0000000000e1')
    on conflict (user_id) do update set motion_weight = 0.9$$,
  '42501', null, 'the demo account should not be able to upsert its score weights'
);
select is_empty(
  $$update public.score_weights set motion_weight = 0.9
    where user_id = '00000000-0000-0000-0000-0000000000e1' returning 1$$,
  'the demo account should not be able to update its score weights'
);
select is_empty(
  $$delete from public.score_weights
    where user_id = '00000000-0000-0000-0000-0000000000e1' returning 1$$,
  'the demo account should not be able to delete its score weights'
);
-- Already refused by table grants for every account; asserted here so a later
-- grant cannot quietly open them to the demo account.
select throws_ok(
  $$insert into public.segments (device_id, recorded_at, duration_sec, s3_key)
    values ('00000000-0000-0000-0000-0000000000f1', now(), 60, 'f1/x.mp4')$$,
  '42501', null, 'the demo account should not be able to insert segments'
);
select throws_ok(
  $$delete from public.reels where id = '00000000-0000-0000-0000-0000000000d1'$$,
  '42501', null, 'the demo account should not be able to delete reels'
);

select throws_ok(
  $$insert into storage.objects (bucket_id, name)
    values ('segments', '00000000-0000-0000-0000-0000000000f1/20261120_100000.mp4')$$,
  '42501', null, 'the demo account should not be able to upload a segment'
);
select throws_ok(
  $$insert into storage.objects (bucket_id, name)
    values ('segments', '00000000-0000-0000-0000-0000000000f1/FLAG_20261120_090010.marker')$$,
  '42501', null, 'the demo account should not be able to upload a flag marker'
);
select throws_ok(
  $$insert into storage.objects (bucket_id, name)
    values ('reels', '00000000-0000-0000-0000-0000000000f1/20261121.mp4')$$,
  '42501', null, 'the demo account should not be able to upload a reel'
);
select is_empty(
  $$update storage.objects set name = '00000000-0000-0000-0000-0000000000f1/renamed.mp4'
    where name = '00000000-0000-0000-0000-0000000000f1/20261120.mp4' returning 1$$,
  'the demo account should not be able to move or overwrite a stored object'
);
select is_empty(
  $$delete from storage.objects
    where name like '00000000-0000-0000-0000-0000000000f1/%' returning 1$$,
  'the demo account should not be able to delete stored objects'
);

-- Controls: the same writes succeed for an ordinary owner, so each denial
-- above is the read-only flag and nothing else.
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000e2", "role": "authenticated"}';

select is(private.is_read_only_user(), false, 'an ordinary owner should not be read-only');
select lives_ok(
  $$insert into public.devices (user_id, name)
    values ('00000000-0000-0000-0000-0000000000e2', 'second owner device')$$,
  'an ordinary owner should be able to add a device'
);
select isnt_empty(
  $$update public.devices set name = 'renamed'
    where device_id = '00000000-0000-0000-0000-0000000000f2' returning 1$$,
  'an ordinary owner should be able to rename their device'
);
select lives_ok(
  $$insert into public.device_status (device_id) values ('00000000-0000-0000-0000-0000000000f2')
    on conflict (device_id) do update set battery_pct = 1$$,
  'an ordinary owner should be able to upsert their device status'
);
select isnt_empty(
  $$update public.segments set user_feedback = 'include'
    where id = '00000000-0000-0000-0000-0000000000c2' returning 1$$,
  'an ordinary owner should be able to set segment feedback'
);
select isnt_empty(
  $$update public.score_weights set motion_weight = 0.9
    where user_id = '00000000-0000-0000-0000-0000000000e2' returning 1$$,
  'an ordinary owner should be able to update their score weights'
);
select lives_ok(
  $$insert into storage.objects (bucket_id, name)
    values ('segments', '00000000-0000-0000-0000-0000000000f2/20261120_100000.mp4')$$,
  'an ordinary owner should be able to upload a segment'
);
select isnt_empty(
  $$delete from storage.objects
    where name = '00000000-0000-0000-0000-0000000000f2/20261120_100000.mp4' returning 1$$,
  'an ordinary owner should be able to delete their stored object'
);
select isnt_empty(
  $$delete from public.devices
    where user_id = '00000000-0000-0000-0000-0000000000e2' and name = 'second owner device'
    returning 1$$,
  'an ordinary owner should be able to delete their device'
);

reset role;

-- Nothing the demo account attempted changed a row.
select is(
  (select name from public.devices where device_id = '00000000-0000-0000-0000-0000000000f1'),
  'demo-device',
  'the demo device should be unchanged'
);
select is(
  (select count(*)::int from storage.objects
    where name like '00000000-0000-0000-0000-0000000000f1/%'),
  2,
  'the demo device''s stored objects should be unchanged'
);

-- The login itself. GoTrue writes auth.users as supabase_auth_admin for both
-- the user's own /user endpoint and the admin API, so the trigger cannot tell
-- them apart by role; it refuses credential and profile changes while the
-- account stays read-only, and lets an update that clears the flag through.
-- Only the admin API (service_role) can change raw_app_meta_data. These run
-- as the test's own role; a trigger fires for every role, GoTrue's included.

select throws_ok(
  $$update auth.users set encrypted_password = 'attacker-hash'
    where id = '00000000-0000-0000-0000-0000000000e1'$$,
  '42501', null, 'the demo account should not be able to change its password'
);
select throws_ok(
  $$update auth.users set email = 'attacker@example.com'
    where id = '00000000-0000-0000-0000-0000000000e1'$$,
  '42501', null, 'the demo account should not be able to change its email'
);
select throws_ok(
  $$update auth.users set email_change = 'attacker@example.com',
                          email_change_token_new = 'token'
    where id = '00000000-0000-0000-0000-0000000000e1'$$,
  '42501', null, 'the demo account should not be able to start an email change'
);
select throws_ok(
  $$update auth.users set raw_user_meta_data = '{"name": "defaced"}'
    where id = '00000000-0000-0000-0000-0000000000e1'$$,
  '42501', null, 'the demo account should not be able to edit its profile'
);
select throws_ok(
  $$insert into auth.mfa_factors (id, user_id, friendly_name, factor_type, status, created_at, updated_at)
    values (gen_random_uuid(), '00000000-0000-0000-0000-0000000000e1', 'lockout', 'totp',
            'unverified', now(), now())$$,
  '42501', null, 'the demo account should not be able to enroll an MFA factor'
);
select lives_ok(
  $$update auth.users set last_sign_in_at = now()
    where id = '00000000-0000-0000-0000-0000000000e1'$$,
  'signing in should still record the demo account''s sign-in time'
);
select lives_ok(
  $$update auth.users set encrypted_password = 'rotated-hash',
                          raw_app_meta_data = raw_app_meta_data - 'read_only'
    where id = '00000000-0000-0000-0000-0000000000e1'$$,
  'an admin update that clears the read-only flag should be able to rotate the password'
);
select lives_ok(
  $$update auth.users set encrypted_password = 'new-owner-hash', email = 'owner2@example.com'
    where id = '00000000-0000-0000-0000-0000000000e2'$$,
  'an ordinary owner should be able to change their password and email'
);

select * from finish();
rollback;
