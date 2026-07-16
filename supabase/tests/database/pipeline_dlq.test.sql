begin;
insert into auth.users (id, email) values
  ('00000000-0000-0000-0000-0000000000d1', 'dlq-owner@example.com');
insert into public.devices (device_id, user_id, name) values
  ('00000000-0000-0000-0000-0000000000d2', '00000000-0000-0000-0000-0000000000d1', 'dlq test device');

select plan(6);

select has_table('public', 'pipeline_dlq', 'pipeline_dlq table should exist');
select col_is_pk(
  'public', 'pipeline_dlq', array['device_id', 'key'],
  'device_id + key should be the composite primary key'
);
select col_is_fk('public', 'pipeline_dlq', 'device_id', 'device_id should reference devices');
select col_type_is('public', 'pipeline_dlq', 'attempts', 'integer', 'attempts should be integer');
select col_default_is('public', 'pipeline_dlq', 'escalated', 'false', 'escalated should default to false');

select throws_ok(
  $$insert into public.pipeline_dlq (device_id, key, kind)
    values ('00000000-0000-0000-0000-0000000000d2', 'k', 'bogus')$$,
  '23514',
  null,
  'kind outside rejected/unmatched should violate the check constraint'
);

select * from finish();
rollback;
