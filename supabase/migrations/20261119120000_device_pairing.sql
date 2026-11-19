-- The pendant gets a login of its own instead of borrowing the owner's session.
-- The owner's app makes a one-time pairing code, the pendant signs in
-- anonymously and claims it, and from then on RLS lets that anonymous "device
-- user" write only its own device's status row and storage folder.
-- docs/superpowers/specs/2026-11-19-visio-device-pairing-design.md

-- devices.device_user_id: unique makes "one device user is one pendant on one
-- device" a database fact; on delete set null means deleting the device user
-- unpairs the device and never deletes the device or its footage.
alter table public.devices
  add column device_user_id uuid unique references auth.users(id) on delete set null;

-- private: PostgREST exposes only public and graphql_public, so nothing here is
-- reachable as a table or an RPC.
create schema if not exists private;
grant usage on schema private to authenticated, service_role;

-- Only sha256(code) is stored. The code carries 256 bits of entropy, so a fast
-- hash is the right tool. No policies and no grants: only the SECURITY DEFINER
-- functions below touch this table.
create table private.device_pairing_codes (
  id uuid primary key default gen_random_uuid(),
  code_hash bytea not null unique,
  user_id uuid not null references auth.users(id) on delete cascade,
  device_id uuid references public.devices(device_id) on delete cascade,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  claimed_at timestamptz,
  claimed_by uuid references auth.users(id) on delete set null
);
create index device_pairing_codes_user_id_idx on private.device_pairing_codes (user_id);
create index device_pairing_codes_device_id_idx on private.device_pairing_codes (device_id);
create index device_pairing_codes_claimed_by_idx on private.device_pairing_codes (claimed_by);
alter table private.device_pairing_codes enable row level security;

-- The single source of "who may act for which device". Every table and storage
-- policy below is written in terms of these. They are SECURITY DEFINER because
-- a device user cannot see any devices row, and they read devices at query
-- time, so revoking a pairing takes effect on the pendant's next request.
create function private.is_anonymous() returns boolean
  language sql stable set search_path = ''
  as $$ select coalesce((auth.jwt() ->> 'is_anonymous')::boolean, false) $$;

create function private.owned_device_ids() returns setof uuid
  language sql stable security definer set search_path = ''
  as $$ select d.device_id from public.devices d where d.user_id = (select auth.uid()) $$;

create function private.paired_device_id() returns uuid
  language sql stable security definer set search_path = ''
  as $$ select d.device_id from public.devices d where d.device_user_id = (select auth.uid()) $$;

revoke all on function private.is_anonymous(), private.owned_device_ids(), private.paired_device_id() from public;
grant execute on function private.is_anonymous(), private.owned_device_ids(), private.paired_device_id()
  to authenticated, service_role;

-- create_pairing_code: called by the owner's app. p_device_id set pairs a
-- pendant to an existing device (Re-onboard keeps footage history); null
-- creates a device when the pendant claims (first pairing). The plaintext
-- exists only in the return value; an account has at most one live code.
create function public.create_pairing_code(p_device_id uuid default null)
  returns table (code text, expires_at timestamptz)
  language plpgsql security definer set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_code text;
  v_expires timestamptz := now() + interval '10 minutes';
begin
  if v_uid is null or not exists (
    select 1 from auth.users u where u.id = v_uid and not u.is_anonymous
  ) then
    raise exception 'only a signed-in account can create a pairing code' using errcode = '42501';
  end if;
  if p_device_id is not null and not exists (
    select 1 from public.devices d where d.device_id = p_device_id and d.user_id = v_uid
  ) then
    raise exception 'device not found' using errcode = '42501';
  end if;
  delete from private.device_pairing_codes c where c.user_id = v_uid and c.claimed_at is null;
  v_code := translate(encode(extensions.gen_random_bytes(32), 'base64'), '+/=', '-_');
  insert into private.device_pairing_codes (code_hash, user_id, device_id, expires_at)
  values (extensions.digest(v_code, 'sha256'), v_uid, p_device_id, v_expires);
  return query select v_code, v_expires;
end;
$$;

-- claim_pairing: called by the pendant as its anonymous device user. It takes
-- only the code, so a pendant can never name a device. Unknown, expired and
-- used codes share one error. Idempotent for the device user that claimed,
-- so a pendant that lost power after the claim committed can ask again.
-- Re-pairing deletes the device's previous device user in the same transaction.
create function public.claim_pairing(p_code text)
  returns uuid
  language plpgsql security definer set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_row private.device_pairing_codes%rowtype;
  v_device_id uuid;
  v_previous uuid;
begin
  if v_uid is null or not exists (
    select 1 from auth.users u where u.id = v_uid and u.is_anonymous
  ) then
    raise exception 'only a device session can claim a pairing code' using errcode = '42501';
  end if;
  select * into v_row from private.device_pairing_codes c
    where c.code_hash = extensions.digest(p_code, 'sha256') for update;
  if found and v_row.claimed_by = v_uid then
    return v_row.device_id;
  end if;
  if not found or v_row.claimed_at is not null or v_row.expires_at <= now() then
    raise exception 'invalid pairing code' using errcode = 'P0001';
  end if;
  if exists (select 1 from public.devices d where d.device_user_id = v_uid) then
    raise exception 'this device session is already paired' using errcode = 'P0001';
  end if;
  if v_row.device_id is null then
    insert into public.devices (user_id, name, device_user_id)
    values (v_row.user_id, 'visio-pendant', v_uid)
    returning device_id into v_device_id;
  else
    select d.device_user_id into v_previous from public.devices d
      where d.device_id = v_row.device_id and d.user_id = v_row.user_id for update;
    if not found then
      raise exception 'invalid pairing code' using errcode = 'P0001';
    end if;
    v_device_id := v_row.device_id;
    update public.devices set device_user_id = v_uid where device_id = v_device_id;
    if v_previous is not null then
      delete from auth.users u where u.id = v_previous and u.is_anonymous;
    end if;
  end if;
  update private.device_pairing_codes
    set claimed_at = now(), claimed_by = v_uid, device_id = v_device_id
    where id = v_row.id;
  return v_device_id;
end;
$$;

revoke all on function public.create_pairing_code(uuid), public.claim_pairing(text) from public, anon;
grant execute on function public.create_pairing_code(uuid), public.claim_pairing(text) to authenticated;
