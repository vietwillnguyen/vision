begin;
insert into auth.users (id, email) values
  ('00000000-0000-0000-0000-0000000000a1', 'owner@example.com'),
  ('00000000-0000-0000-0000-0000000000a2', 'other@example.com');
insert into public.devices (device_id, user_id, name) values
  ('00000000-0000-0000-0000-0000000000b1', '00000000-0000-0000-0000-0000000000a1', 'owner-device');
insert into public.device_status (device_id) values
  ('00000000-0000-0000-0000-0000000000b1');
insert into public.segments (id, device_id, recorded_at, duration_sec, s3_key) values
  ('00000000-0000-0000-0000-0000000000c1', '00000000-0000-0000-0000-0000000000b1', now(), 60, 'b1/seg1.mp4');
insert into public.reels (id, device_id, date, s3_key, duration_sec) values
  ('00000000-0000-0000-0000-0000000000d1', '00000000-0000-0000-0000-0000000000b1', current_date, 'b1/reel1.mp4', 90);
insert into public.score_weights (user_id) values
  ('00000000-0000-0000-0000-0000000000a1');

select plan(18);

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

select throws_ok(
  $$update public.device_status set battery_pct = 100
    where device_id = '00000000-0000-0000-0000-0000000000b1'$$,
  '42501',
  null,
  'the owning user should not be able to update device status directly'
);

select * from finish();
rollback;
