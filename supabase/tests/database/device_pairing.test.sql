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
  ('00000000-0000-0000-0000-0000000000e3', null, true),
  ('00000000-0000-0000-0000-0000000000f1', null, true),
  ('00000000-0000-0000-0000-0000000000f2', null, true),
  ('00000000-0000-0000-0000-0000000000f3', null, true);
insert into public.devices (device_id, user_id, name, device_user_id) values
  ('00000000-0000-0000-0000-0000000000b1', '00000000-0000-0000-0000-0000000000a1', 'owner-device', '00000000-0000-0000-0000-0000000000e1'),
  ('00000000-0000-0000-0000-0000000000b2', '00000000-0000-0000-0000-0000000000a1', 'owner-device-2', null),
  ('00000000-0000-0000-0000-0000000000b3', '00000000-0000-0000-0000-0000000000a2', 'other-device', null);

-- Plaintext codes, passed between roles inside this one transaction.
create temp table codes (label text primary key, code text, expires_at timestamptz);
grant all on codes to authenticated;

select plan(38);

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


-- create_pairing_code

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000a1", "role": "authenticated"}';
insert into codes select 'first', * from public.create_pairing_code('00000000-0000-0000-0000-0000000000b2');
select is((select count(*)::int from codes where label = 'first'), 1, 'create_pairing_code returns one row');
select is((select length(code) from codes where label = 'first'), 43, 'the code is 43 base64url characters');
select ok(
  (select expires_at between now() + interval '9 minutes' and now() + interval '11 minutes'
     from codes where label = 'first'),
  'the code expires in 10 minutes'
);
select throws_ok(
  $$select public.create_pairing_code('00000000-0000-0000-0000-0000000000b3')$$,
  '42501', null,
  'an account cannot make a code for a device it does not own'
);
insert into codes select 'second', * from public.create_pairing_code('00000000-0000-0000-0000-0000000000b2');

set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000f1", "role": "authenticated", "is_anonymous": true}';
select throws_ok(
  $$select public.create_pairing_code(null)$$,
  '42501', null,
  'an anonymous user cannot make a pairing code'
);
set local request.jwt.claims = '{"role": "authenticated"}';
select throws_ok(
  $$select public.create_pairing_code(null)$$,
  '42501', null,
  'a caller with no user cannot make a pairing code'
);

reset role;
select ok(
  exists (
    select 1 from private.device_pairing_codes c join codes t on t.label = 'second'
    where c.code_hash = extensions.digest(t.code, 'sha256')
      and c.code_hash <> convert_to(t.code, 'UTF8')
  ),
  'the stored row holds the sha256 of the code and not the code'
);
select results_eq(
  $$select c.code_hash from private.device_pairing_codes c
    where c.user_id = '00000000-0000-0000-0000-0000000000a1' and c.claimed_at is null$$,
  $$select extensions.digest(code, 'sha256') from codes where label = 'second'$$,
  'a new code replaces the account''s unclaimed one'
);

-- claim_pairing

set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000f1", "role": "authenticated", "is_anonymous": true}';
select is(
  public.claim_pairing((select code from codes where label = 'second')),
  '00000000-0000-0000-0000-0000000000b2'::uuid,
  'claiming returns the device the owner chose'
);
select is(
  public.claim_pairing((select code from codes where label = 'second')),
  '00000000-0000-0000-0000-0000000000b2'::uuid,
  'claiming again as the same device user returns the same device'
);
select throws_ok(
  $$select public.claim_pairing((select code from codes where label = 'first'))$$,
  'P0001', 'invalid pairing code',
  'a replaced code is invalid'
);

set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000f2", "role": "authenticated", "is_anonymous": true}';
select throws_ok(
  $$select public.claim_pairing((select code from codes where label = 'second'))$$,
  'P0001', 'invalid pairing code',
  'a used code is invalid for any other device user'
);
select throws_ok(
  $$select public.claim_pairing('not-a-real-code')$$,
  'P0001', 'invalid pairing code',
  'an unknown code gets the same error'
);

set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000a2", "role": "authenticated"}';
select throws_ok(
  $$select public.claim_pairing((select code from codes where label = 'second'))$$,
  '42501', null,
  'a non-anonymous account cannot claim a code'
);

-- Expired: aged as postgres, since expiry cannot be aged through the API.
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000a1", "role": "authenticated"}';
insert into codes select 'expired', * from public.create_pairing_code('00000000-0000-0000-0000-0000000000b2');
reset role;
update private.device_pairing_codes set expires_at = now() - interval '1 second'
  where code_hash = (select extensions.digest(code, 'sha256') from codes where label = 'expired');
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000f2", "role": "authenticated", "is_anonymous": true}';
select throws_ok(
  $$select public.claim_pairing((select code from codes where label = 'expired'))$$,
  'P0001', 'invalid pairing code',
  'an expired code gets the same error'
);

reset role;
select results_eq(
  $$select device_user_id from public.devices where device_id = '00000000-0000-0000-0000-0000000000b2'$$,
  $$values ('00000000-0000-0000-0000-0000000000f1'::uuid)$$,
  'claiming linked the device to the claiming device user'
);

-- A code with no device creates one for the code's creator.
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000a2", "role": "authenticated"}';
insert into codes select 'new-device', * from public.create_pairing_code(null);
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000f3", "role": "authenticated", "is_anonymous": true}';
create temp table new_device as
  select public.claim_pairing((select code from codes where label = 'new-device')) as device_id;
reset role;
select results_eq(
  $$select d.user_id, d.name, d.device_user_id from public.devices d join new_device n using (device_id)$$,
  $$values ('00000000-0000-0000-0000-0000000000a2'::uuid, 'visio-pendant'::text, '00000000-0000-0000-0000-0000000000f3'::uuid)$$,
  'a code with no device creates a device owned by its creator and linked to the claimer'
);

-- Re-pair: a new pendant on b2 replaces f1 and deletes it.
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000a1", "role": "authenticated"}';
insert into codes select 'repair', * from public.create_pairing_code('00000000-0000-0000-0000-0000000000b2');
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000f2", "role": "authenticated", "is_anonymous": true}';
select is(
  public.claim_pairing((select code from codes where label = 'repair')),
  '00000000-0000-0000-0000-0000000000b2'::uuid,
  're-pairing returns the same device'
);
reset role;
select results_eq(
  $$select device_user_id from public.devices where device_id = '00000000-0000-0000-0000-0000000000b2'$$,
  $$values ('00000000-0000-0000-0000-0000000000f2'::uuid)$$,
  're-pairing links the new device user'
);
select is_empty(
  $$select 1 from auth.users where id = '00000000-0000-0000-0000-0000000000f1'$$,
  're-pairing deletes the previous device user'
);

-- A device user that is already linked cannot claim a second code.
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000a1", "role": "authenticated"}';
insert into codes select 'second-claim', * from public.create_pairing_code('00000000-0000-0000-0000-0000000000b2');
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-0000000000f3", "role": "authenticated", "is_anonymous": true}';
select throws_ok(
  $$select public.claim_pairing((select code from codes where label = 'second-claim'))$$,
  'P0001', null,
  'a linked device user cannot claim a second code'
);
reset role;

select ok(
  not has_function_privilege('anon', 'public.create_pairing_code(uuid)', 'execute')
    and not has_function_privilege('anon', 'public.claim_pairing(text)', 'execute'),
  'anon cannot execute the pairing functions'
);

select * from finish();
rollback;
