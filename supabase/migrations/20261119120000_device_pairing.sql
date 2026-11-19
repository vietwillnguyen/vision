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
