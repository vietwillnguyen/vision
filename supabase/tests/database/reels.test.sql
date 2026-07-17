begin;
select plan(4);

select has_table('public', 'reels', 'reels table should exist');
select col_is_fk('public', 'reels', 'device_id', 'device_id should reference devices');
select col_type_is('public', 'reels', 'date', 'date', 'date column should be type date');
select col_is_unique(
  'public', 'reels', array['device_id', 'date'],
  'device_id + date should be unique so same-day re-runs upsert one reel'
);

select * from finish();
rollback;
