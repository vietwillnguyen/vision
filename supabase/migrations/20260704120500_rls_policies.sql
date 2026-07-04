-- App roles need table privileges before RLS policies can grant row visibility:
-- Postgres checks GRANTs first, then filters rows through RLS.
grant select, insert, update, delete
  on public.devices, public.device_status, public.segments, public.reels, public.score_weights
  to authenticated, service_role;

create policy "devices_owner_access" on public.devices
  for all using (auth.uid() = user_id);

create policy "device_status_owner_access" on public.device_status
  for all using (
    auth.uid() = (select user_id from public.devices d where d.device_id = device_status.device_id)
  );

create policy "segments_owner_access" on public.segments
  for all using (
    auth.uid() = (select user_id from public.devices d where d.device_id = segments.device_id)
  );

create policy "reels_owner_access" on public.reels
  for all using (
    auth.uid() = (select user_id from public.devices d where d.device_id = reels.device_id)
  );

create policy "score_weights_owner_access" on public.score_weights
  for all using (auth.uid() = user_id);
