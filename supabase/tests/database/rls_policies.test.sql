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
