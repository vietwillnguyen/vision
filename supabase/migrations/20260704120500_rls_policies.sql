-- App roles need table privileges before RLS policies can grant row visibility:
-- Postgres checks GRANTs first, then filters rows through RLS.
-- The pipeline writes through service_role; authenticated users manage their own
-- devices and score weights, and flag segments via user_feedback.
-- (device_status write access for the firmware daemon, which authenticates as
-- authenticated rather than service_role, is granted separately - see
-- 20260710090000_grant_device_status_writes.sql.)
grant select, insert, update, delete
  on public.devices, public.device_status, public.segments, public.reels, public.score_weights
  to service_role;

grant select
  on public.devices, public.device_status, public.segments, public.reels, public.score_weights
  to authenticated;

grant insert, update, delete on public.devices, public.score_weights to authenticated;

grant update (user_feedback) on public.segments to authenticated;

create policy "devices_owner_access" on public.devices
  for all using ((select auth.uid()) = user_id);

create policy "device_status_owner_access" on public.device_status
  for all using (
    (select auth.uid()) = (select user_id from public.devices d where d.device_id = device_status.device_id)
  );

create policy "segments_owner_access" on public.segments
  for all using (
    (select auth.uid()) = (select user_id from public.devices d where d.device_id = segments.device_id)
  );

create policy "reels_owner_access" on public.reels
  for all using (
    (select auth.uid()) = (select user_id from public.devices d where d.device_id = reels.device_id)
  );

create policy "score_weights_owner_access" on public.score_weights
  for all using ((select auth.uid()) = user_id);
