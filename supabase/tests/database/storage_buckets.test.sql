begin;
insert into auth.users (id, email) values
  ('00000000-0000-0000-0000-0000000000a1', 'owner@example.com'),
  ('00000000-0000-0000-0000-0000000000a2', 'other@example.com');
insert into public.devices (device_id, user_id, name) values
  ('00000000-0000-0000-0000-0000000000b1', '00000000-0000-0000-0000-0000000000a1', 'owner-device');

select plan(8);

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

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000a1", "role": "authenticated"}';

select lives_ok(
  $$insert into storage.objects (bucket_id, name) values
    ('segments', '00000000-0000-0000-0000-0000000000b1/seg1.mp4')$$,
  'the device owner should be able to upload to their segments prefix'
);

select lives_ok(
  $$insert into storage.objects (bucket_id, name) values
    ('reels', '00000000-0000-0000-0000-0000000000b1/reel1.mp4')$$,
  'the device owner should be able to upload to their reels prefix'
);

select isnt_empty(
  $$select 1 from storage.objects where bucket_id = 'segments'
    and name = '00000000-0000-0000-0000-0000000000b1/seg1.mp4'$$,
  'the device owner should see objects under their segments prefix'
);

set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000a2", "role": "authenticated"}';

select throws_ok(
  $$insert into storage.objects (bucket_id, name) values
    ('segments', '00000000-0000-0000-0000-0000000000b1/intruder.mp4')$$,
  '42501',
  null,
  'a different user should not be able to upload to another user''s segments prefix'
);

select throws_ok(
  $$insert into storage.objects (bucket_id, name) values
    ('reels', '00000000-0000-0000-0000-0000000000b1/intruder.mp4')$$,
  '42501',
  null,
  'a different user should not be able to upload to another user''s reels prefix'
);

select is_empty(
  $$select 1 from storage.objects where bucket_id = 'segments'
    and name = '00000000-0000-0000-0000-0000000000b1/seg1.mp4'$$,
  'a different user should not see objects under another user''s prefix'
);

select * from finish();
rollback;
