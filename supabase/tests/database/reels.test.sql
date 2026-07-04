begin;
select plan(3);

select has_table('public', 'reels', 'reels table should exist');
select col_is_fk('public', 'reels', 'device_id', 'device_id should reference devices');
select col_type_is('public', 'reels', 'date', 'date', 'date column should be type date');

select * from finish();
rollback;
