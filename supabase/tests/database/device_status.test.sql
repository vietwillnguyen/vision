begin;
select plan(4);

select has_table('public', 'device_status', 'device_status table should exist');
select col_is_pk('public', 'device_status', 'device_id', 'device_id should be the primary key');
select col_is_fk('public', 'device_status', 'device_id', 'device_id should reference devices');
select col_type_is('public', 'device_status', 'battery_pct', 'integer', 'battery_pct should be integer');

select * from finish();
rollback;
