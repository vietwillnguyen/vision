-- The pendant's own login: the device user link, the hashed pairing-code
-- table, the permission helpers and the pairing functions.
-- docs/superpowers/specs/2026-11-19-visio-device-pairing-design.md
begin;

-- a1 owner, a2 another account, e1/e2/e3 anonymous device users (e3 never linked).
insert into auth.users (id, email, is_anonymous) values
  ('00000000-0000-0000-0000-0000000000a1', 'owner@example.com', false),
  ('00000000-0000-0000-0000-0000000000a2', 'other@example.com', false),
  ('00000000-0000-0000-0000-0000000000e1', null, true),
  ('00000000-0000-0000-0000-0000000000e2', null, true),
  ('00000000-0000-0000-0000-0000000000e3', null, true);
insert into public.devices (device_id, user_id, name, device_user_id) values
  ('00000000-0000-0000-0000-0000000000b1', '00000000-0000-0000-0000-0000000000a1', 'owner-device', '00000000-0000-0000-0000-0000000000e1'),
  ('00000000-0000-0000-0000-0000000000b2', '00000000-0000-0000-0000-0000000000a1', 'owner-device-2', null),
  ('00000000-0000-0000-0000-0000000000b3', '00000000-0000-0000-0000-0000000000a2', 'other-device', null);

select plan(16);

-- devices.device_user_id

select has_column('public', 'devices', 'device_user_id', 'devices should have device_user_id');
select col_type_is('public', 'devices', 'device_user_id', 'uuid', 'device_user_id should be uuid');
select col_is_unique('public', 'devices', 'device_user_id', 'one device user is one pendant on one device');

-- private schema and the pairing-code table

select has_table('private', 'device_pairing_codes', 'the pairing-code table should exist');
select ok(
  (select relrowsecurity from pg_class where oid = 'private.device_pairing_codes'::regclass),
  'RLS should be enabled on the pairing-code table'
);
select ok(
  not has_table_privilege('authenticated', 'private.device_pairing_codes', 'select'),
  'authenticated should have no select on the pairing-code table'
);
select ok(
  not has_table_privilege('anon', 'private.device_pairing_codes', 'select'),
  'anon should have no select on the pairing-code table'
);

-- helpers

select has_function('private', 'owned_device_ids', 'private.owned_device_ids should exist');
select has_function('private', 'paired_device_id', 'private.paired_device_id should exist');
select has_function('private', 'is_anonymous', 'private.is_anonymous should exist');

set local role authenticated;

set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000a1", "role": "authenticated"}';
select set_eq(
  $$select private.owned_device_ids()$$,
  $$values ('00000000-0000-0000-0000-0000000000b1'::uuid), ('00000000-0000-0000-0000-0000000000b2'::uuid)$$,
  'owned_device_ids returns exactly the caller''s devices'
);
select is(private.is_anonymous(), false, 'an owner is not anonymous');

set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000e1", "role": "authenticated", "is_anonymous": true}';
select is(
  private.paired_device_id(),
  '00000000-0000-0000-0000-0000000000b1'::uuid,
  'paired_device_id returns the linked device user''s device'
);
select is(private.is_anonymous(), true, 'a device user is anonymous');

set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000e3", "role": "authenticated", "is_anonymous": true}';
select is(private.paired_device_id(), null, 'an unlinked anonymous user has no paired device');

reset role;

-- Deleting the device user unpairs the device and keeps the device row.
delete from auth.users where id = '00000000-0000-0000-0000-0000000000e1';
select results_eq(
  $$select device_user_id from public.devices where device_id = '00000000-0000-0000-0000-0000000000b1'$$,
  $$values (null::uuid)$$,
  'deleting the device user clears device_user_id and keeps the device'
);

select * from finish();
rollback;
