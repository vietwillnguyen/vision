begin;
-- a1 owner, a2 another account, e1 the pendant's device user for b1, e9 an
-- anonymous user that never paired, e8 the device user of an earlier pairing.
insert into auth.users (id, email, is_anonymous) values
  ('00000000-0000-0000-0000-0000000000a1', 'owner@example.com', false),
  ('00000000-0000-0000-0000-0000000000a2', 'other@example.com', false),
  ('00000000-0000-0000-0000-0000000000e1', null, true),
  ('00000000-0000-0000-0000-0000000000e9', null, true);
insert into public.devices (device_id, user_id, name, device_user_id) values
  ('00000000-0000-0000-0000-0000000000b1', '00000000-0000-0000-0000-0000000000a1', 'owner-device', '00000000-0000-0000-0000-0000000000e1'),
  ('00000000-0000-0000-0000-0000000000b2', '00000000-0000-0000-0000-0000000000a2', 'other-device', null);
-- Stored before this test: a segment from an earlier pairing and a reel.
insert into storage.objects (bucket_id, name, owner_id) values
  ('segments', '00000000-0000-0000-0000-0000000000b1/earlier.mp4', '00000000-0000-0000-0000-0000000000e8'),
  ('reels', '00000000-0000-0000-0000-0000000000b1/reel1.mp4', null);

select plan(28);

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
-- The hosted free plan rejects any object over 50 MiB and cannot be raised.
-- The local stack's [storage] file_size_limit is not enforced by the CLI's
-- storage container, so the buckets carry the cap themselves.
select is(
  (select file_size_limit from storage.buckets where id = 'segments'),
  52428800::bigint,
  'segments bucket should cap objects at the hosted 50 MiB upload limit'
);
select is(
  (select file_size_limit from storage.buckets where id = 'reels'),
  52428800::bigint,
  'reels bucket should cap objects at the hosted 50 MiB upload limit'
);

set local role authenticated;

-- The pendant's device user uploads into its own device's segments folder.
-- storage-api sets owner_id to the uploader's auth.uid().

set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000e1", "role": "authenticated", "is_anonymous": true}';

select lives_ok(
  $$insert into storage.objects (bucket_id, name, owner_id) values
    ('segments', '00000000-0000-0000-0000-0000000000b1/seg1.mp4', '00000000-0000-0000-0000-0000000000e1')$$,
  'the device user should be able to upload to its device''s segments folder'
);

select throws_ok(
  $$insert into storage.objects (bucket_id, name, owner_id) values
    ('segments', '00000000-0000-0000-0000-0000000000b2/intruder.mp4', '00000000-0000-0000-0000-0000000000e1')$$,
  '42501',
  null,
  'the device user should not be able to upload to another device''s folder'
);

select throws_ok(
  $$insert into storage.objects (bucket_id, name, owner_id) values
    ('reels', '00000000-0000-0000-0000-0000000000b1/reel2.mp4', '00000000-0000-0000-0000-0000000000e1')$$,
  '42501',
  null,
  'the device user should not be able to upload reels'
);

select throws_ok(
  $$insert into storage.objects (bucket_id, name, owner_id) values
    ('segments', '00000000-0000-0000-0000-0000000000b1/spoof.mp4', '00000000-0000-0000-0000-0000000000a1')$$,
  '42501',
  null,
  'the device user should not be able to upload as somebody else'
);

-- Crafted keys: the folder check is an exact text match on the first folder.
select throws_ok(
  $$insert into storage.objects (bucket_id, name, owner_id) values
    ('segments', 'no-folder.mp4', '00000000-0000-0000-0000-0000000000e1')$$,
  '42501',
  null,
  'the device user should not be able to upload outside any folder'
);

select throws_ok(
  $$insert into storage.objects (bucket_id, name, owner_id) values
    ('segments', '00000000-0000-0000-0000-0000000000b1x/seg.mp4', '00000000-0000-0000-0000-0000000000e1')$$,
  '42501',
  null,
  'the device user should not be able to upload under a folder that extends its device id'
);

select throws_ok(
  $$insert into storage.objects (bucket_id, name, owner_id) values
    ('segments', '00000000-0000-0000-0000-0000000000B1/seg.mp4', '00000000-0000-0000-0000-0000000000e1')$$,
  '42501',
  null,
  'the device user should not be able to upload under an uppercased device id'
);

select isnt_empty(
  $$select 1 from storage.objects where bucket_id = 'segments'
    and name = '00000000-0000-0000-0000-0000000000b1/seg1.mp4'$$,
  'the device user should see the objects it uploaded'
);

-- Uploads are write-once for the pendant: no update or delete policy.
select is_empty(
  $$with u as (
      update storage.objects set name = '00000000-0000-0000-0000-0000000000b1/renamed.mp4'
      where bucket_id = 'segments' and name = '00000000-0000-0000-0000-0000000000b1/seg1.mp4'
      returning 1
    ) select * from u$$,
  'the device user should not be able to update its own uploads'
);

-- storage-api sets this for its own deletes; without it the protect_delete
-- trigger refuses every SQL delete, so RLS alone decides here, as it does for
-- a delete through the API.
set local storage.allow_delete_query = 'true';
select is_empty(
  $$with d as (
      delete from storage.objects
      where bucket_id = 'segments' and name = '00000000-0000-0000-0000-0000000000b1/seg1.mp4'
      returning 1
    ) select * from d$$,
  'the device user should not be able to delete its own uploads'
);
set local storage.allow_delete_query = 'false';

select is_empty(
  $$select 1 from storage.objects where bucket_id = 'segments'
    and name = '00000000-0000-0000-0000-0000000000b1/earlier.mp4'$$,
  'the device user should not see footage uploaded under an earlier pairing'
);

select is_empty(
  $$select 1 from storage.objects where bucket_id = 'reels'$$,
  'the device user should not see reels'
);

-- An anonymous user that never paired can do nothing.

set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000e9", "role": "authenticated", "is_anonymous": true}';

select throws_ok(
  $$insert into storage.objects (bucket_id, name, owner_id) values
    ('segments', '00000000-0000-0000-0000-0000000000b1/unpaired.mp4', '00000000-0000-0000-0000-0000000000e9')$$,
  '42501',
  null,
  'an unpaired anonymous user should not be able to upload'
);

select is_empty(
  $$select 1 from storage.objects$$,
  'an unpaired anonymous user should see no objects'
);

-- The owner reads both buckets for its devices and writes neither.

set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000a1", "role": "authenticated"}';

select set_eq(
  $$select name from storage.objects where bucket_id = 'segments'$$,
  $$values ('00000000-0000-0000-0000-0000000000b1/earlier.mp4'), ('00000000-0000-0000-0000-0000000000b1/seg1.mp4')$$,
  'the device owner should see every object under their segments prefix'
);

select isnt_empty(
  $$select 1 from storage.objects where bucket_id = 'reels'
    and name = '00000000-0000-0000-0000-0000000000b1/reel1.mp4'$$,
  'the device owner should see objects under their reels prefix'
);

select throws_ok(
  $$insert into storage.objects (bucket_id, name, owner_id) values
    ('segments', '00000000-0000-0000-0000-0000000000b1/owner.mp4', '00000000-0000-0000-0000-0000000000a1')$$,
  '42501',
  null,
  'the device owner should not be able to upload segments'
);

select throws_ok(
  $$insert into storage.objects (bucket_id, name, owner_id) values
    ('reels', '00000000-0000-0000-0000-0000000000b1/owner.mp4', '00000000-0000-0000-0000-0000000000a1')$$,
  '42501',
  null,
  'the device owner should not be able to upload reels'
);

-- Another account.

set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000a2", "role": "authenticated"}';

select throws_ok(
  $$insert into storage.objects (bucket_id, name, owner_id) values
    ('segments', '00000000-0000-0000-0000-0000000000b1/intruder.mp4', '00000000-0000-0000-0000-0000000000a2')$$,
  '42501',
  null,
  'a different user should not be able to upload to another user''s segments prefix'
);

select throws_ok(
  $$insert into storage.objects (bucket_id, name, owner_id) values
    ('reels', '00000000-0000-0000-0000-0000000000b1/intruder.mp4', '00000000-0000-0000-0000-0000000000a2')$$,
  '42501',
  null,
  'a different user should not be able to upload to another user''s reels prefix'
);

select is_empty(
  $$select 1 from storage.objects where bucket_id = 'segments'
    and name = '00000000-0000-0000-0000-0000000000b1/seg1.mp4'$$,
  'a different user should not see objects under another user''s prefix'
);

select is_empty(
  $$select 1 from storage.objects where bucket_id = 'reels'$$,
  'a different user should not see another user''s reels'
);

-- Remove device revokes the pendant's storage access immediately.

set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000a1", "role": "authenticated"}';
select public.unpair_device('00000000-0000-0000-0000-0000000000b1');
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000e1", "role": "authenticated", "is_anonymous": true}';

select throws_ok(
  $$insert into storage.objects (bucket_id, name, owner_id) values
    ('segments', '00000000-0000-0000-0000-0000000000b1/after.mp4', '00000000-0000-0000-0000-0000000000e1')$$,
  '42501',
  null,
  'an unpaired device user should not be able to upload any more'
);

select is_empty(
  $$select 1 from storage.objects$$,
  'an unpaired device user should not see its old uploads'
);

select * from finish();
rollback;
