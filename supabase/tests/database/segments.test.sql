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
