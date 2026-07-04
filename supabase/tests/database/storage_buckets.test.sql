begin;
select plan(2);

select is(
  (select public from storage.buckets where id = 'segments'),
  false,
  'segments bucket should be private'
);
select is(
  (select public from storage.buckets where id = 'reels'),
  false,
  'reels bucket should be private'
);

select * from finish();
rollback;
