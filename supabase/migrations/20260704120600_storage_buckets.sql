insert into storage.buckets (id, name, public)
values ('segments', 'segments', false), ('reels', 'reels', false)
on conflict (id) do nothing;

-- Object keys follow the convention {device_id}/{filename}; the policies below
-- resolve the first path folder to public.devices to find the owning user.
create policy "segments_bucket_owner_access" on storage.objects
  for all using (
    bucket_id = 'segments'
    and (select auth.uid()) = (
      select user_id from public.devices d
      where d.device_id::text = (storage.foldername(objects.name))[1]
    )
  );

create policy "reels_bucket_owner_access" on storage.objects
  for all using (
    bucket_id = 'reels'
    and (select auth.uid()) = (
      select user_id from public.devices d
      where d.device_id::text = (storage.foldername(objects.name))[1]
    )
  );
