begin;
select plan(2);

select has_column('public', 'devices', 'push_token', 'devices should have a push_token column');
select col_is_null('public', 'devices', 'push_token', 'push_token should be nullable');

select * from finish();
rollback;
