begin;
select plan(8);

select has_table('public', 'device_status', 'device_status table should exist');
select col_is_pk('public', 'device_status', 'device_id', 'device_id should be the primary key');
select col_is_fk('public', 'device_status', 'device_id', 'device_id should reference devices');
select col_type_is('public', 'device_status', 'battery_pct', 'integer', 'battery_pct should be integer');

select col_type_is('public', 'device_status', 'segments_rejected', 'integer', 'segments_rejected should be integer');
select col_default_is('public', 'device_status', 'segments_rejected', '0', 'segments_rejected should default to 0');

select col_type_is('public', 'device_status', 'audio_active', 'boolean', 'audio_active should be boolean');
select col_default_is('public', 'device_status', 'audio_active', 'false', 'audio_active should default to false');

select * from finish();
rollback;
