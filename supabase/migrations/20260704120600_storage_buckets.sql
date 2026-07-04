insert into storage.buckets (id, name, public)
values ('segments', 'segments', false), ('reels', 'reels', false)
on conflict (id) do nothing;

create policy "segments_bucket_owner_access" on storage.objects
  for all using (
    bucket_id = 'segments'
    and (select auth.uid()) = (
      select user_id from public.devices d
      where d.device_id::text = (storage.foldername(name))[1]
    )
  );

create policy "reels_bucket_owner_access" on storage.objects
  for all using (
    bucket_id = 'reels'
    and (select auth.uid()) = (
      select user_id from public.devices d
      where d.device_id::text = (storage.foldername(name))[1]
    )
  );
