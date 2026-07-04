begin;
select plan(5);

select has_table('public', 'devices', 'devices table should exist');
select has_column('public', 'devices', 'device_id', 'devices should have device_id');
select col_is_pk('public', 'devices', 'device_id', 'device_id should be the primary key');
select col_type_is('public', 'devices', 'user_id', 'uuid', 'user_id should be uuid');
select col_not_null('public', 'devices', 'name', 'name should be not null');

select * from finish();
rollback;
