-- Read-only accounts, for the public demo.
--
-- A stranger taps "View demo" in the web build and is signed in as a shared
-- demo account whose password ships in the public bundle. Anything that
-- account can do, anyone can do, so read-only is enforced here, in the
-- database, and the app's hidden buttons are only a courtesy.
--
-- An account is read-only when auth.users.raw_app_meta_data has
-- "read_only": true. app_metadata is the part of a user GoTrue only lets the
-- admin API (service_role) write, so the account cannot clear its own flag.
-- The flag is read from auth.users on every statement, not from the JWT, so
-- setting or clearing it takes effect immediately rather than at the next
-- token refresh.
--
-- The demo account owns the demo device, so the existing owner policies are
-- what let it read; nothing here grants a read. Writes are refused by:
--   * a restrictive policy per write command on every public table and on
--     storage.objects (restrictive policies are ANDed with the permissive
--     owner policies, so they can only take access away);
--   * triggers on auth.users and auth.mfa_factors, because GoTrue lets any
--     signed-in user change their own password, email and profile, or enroll
--     an MFA factor - each of which would lock every other visitor out.
-- The pgTAP suite read_only_users.test.sql proves each denial and fails if a
-- public table is ever added without its restrictive policies.

-- private: PostgREST exposes only the schemas in config.toml's api.schemas
-- (public, graphql_public), so nothing here is reachable as a table or RPC.
create schema if not exists private;
grant usage on schema private to authenticated, service_role;

-- SECURITY DEFINER because authenticated cannot read auth.users. The
-- comparison is on the text value so a malformed flag reads as "not
-- read-only" instead of raising inside every policy check.
create function private.is_read_only_user() returns boolean
  language sql stable security definer set search_path = ''
  as $$
    select coalesce(
      (select u.raw_app_meta_data ->> 'read_only' = 'true'
         from auth.users u
        where u.id = (select auth.uid())),
      false
    )
  $$;
revoke all on function private.is_read_only_user() from public, anon;
grant execute on function private.is_read_only_user() to authenticated, service_role;

-- One restrictive policy per table and write command. `to authenticated`
-- because that is the only role a signed-in account reaches the database as;
-- anon already has no table grants, and service_role bypasses RLS. The
-- `(select ...)` wrapper lets Postgres evaluate the function once per
-- statement instead of once per row.
do $$
declare
  t text;
begin
  for t in
    select c.relname
      from pg_class c
     where c.relnamespace = 'public'::regnamespace
       and c.relkind = 'r'
  loop
    execute format(
      'create policy %I on public.%I as restrictive for insert to authenticated
         with check (not (select private.is_read_only_user()))',
      t || '_read_only_insert', t);
    execute format(
      'create policy %I on public.%I as restrictive for update to authenticated
         using (not (select private.is_read_only_user()))
         with check (not (select private.is_read_only_user()))',
      t || '_read_only_update', t);
    execute format(
      'create policy %I on public.%I as restrictive for delete to authenticated
         using (not (select private.is_read_only_user()))',
      t || '_read_only_delete', t);
  end loop;
end
$$;

-- Every bucket, not just segments and reels: a read-only account may not
-- upload, overwrite, move or delete any object at all. Storage's resumable
-- (TUS) and S3 multipart paths check these same insert policies.
create policy "objects_read_only_insert" on storage.objects
  as restrictive for insert to authenticated
  with check (not (select private.is_read_only_user()));
create policy "objects_read_only_update" on storage.objects
  as restrictive for update to authenticated
  using (not (select private.is_read_only_user()))
  with check (not (select private.is_read_only_user()));
create policy "objects_read_only_delete" on storage.objects
  as restrictive for delete to authenticated
  using (not (select private.is_read_only_user()));

-- GoTrue writes auth.users as supabase_auth_admin for the user's own /user
-- endpoint and for the admin API alike, so the trigger cannot tell the two
-- apart by role. It refuses credential and profile changes while the account
-- stays read-only. An update that also clears the flag is let through: only
-- the admin API can write raw_app_meta_data, so that is how the seed tool
-- rotates the demo password. Sign-in bookkeeping (last_sign_in_at,
-- updated_at, confirmation timestamps) is untouched by this check.
create function private.guard_read_only_auth_user() returns trigger
  language plpgsql security definer set search_path = ''
  as $$
  begin
    if coalesce(old.raw_app_meta_data ->> 'read_only', '') = 'true'
       and coalesce(new.raw_app_meta_data ->> 'read_only', '') = 'true'
       and (
         new.encrypted_password is distinct from old.encrypted_password
         or new.email is distinct from old.email
         or new.email_change is distinct from old.email_change
         or new.email_change_token_new is distinct from old.email_change_token_new
         or new.email_change_token_current is distinct from old.email_change_token_current
         or new.phone is distinct from old.phone
         or new.phone_change is distinct from old.phone_change
         or new.raw_user_meta_data is distinct from old.raw_user_meta_data
       )
    then
      raise exception 'account % is read-only', old.id using errcode = '42501';
    end if;
    return new;
  end
  $$;
revoke all on function private.guard_read_only_auth_user() from public, anon, authenticated;

create trigger guard_read_only_auth_user
  before update on auth.users
  for each row execute function private.guard_read_only_auth_user();

-- An enrolled factor would make every later demo sign-in demand a code only
-- the enroller has. config.toml leaves MFA enrollment off; this holds even if
-- a project turns it on.
create function private.guard_read_only_mfa_factor() returns trigger
  language plpgsql security definer set search_path = ''
  as $$
  begin
    if exists (
      select 1 from auth.users u
       where u.id = new.user_id
         and coalesce(u.raw_app_meta_data ->> 'read_only', '') = 'true'
    ) then
      raise exception 'account % is read-only', new.user_id using errcode = '42501';
    end if;
    return new;
  end
  $$;
revoke all on function private.guard_read_only_mfa_factor() from public, anon, authenticated;

create trigger guard_read_only_mfa_factor
  before insert on auth.mfa_factors
  for each row execute function private.guard_read_only_mfa_factor();
