begin;
insert into auth.users (id, email) values ('00000000-0000-0000-0000-000000000003', 'weights@example.com');

select plan(3);

select has_table('public', 'score_weights', 'score_weights table should exist');
select col_is_pk('public', 'score_weights', 'user_id', 'user_id should be the primary key');

insert into public.score_weights (user_id) values ('00000000-0000-0000-0000-000000000003');

-- audio_weight is 0, not the spec's 0.3, until the firmware records audio
-- (vision-audio-capture). This must stay in step with
-- ScoreWeights.audio_weight in pipeline/pipeline/models.py.
select results_eq(
  $$select scene_weight, audio_weight, motion_weight from public.score_weights where user_id = '00000000-0000-0000-0000-000000000003'$$,
  $$values (0.4::numeric, 0::numeric, 0.2::numeric)$$,
  'defaults should be 0.4/0/0.2'
);

select * from finish();
rollback;
