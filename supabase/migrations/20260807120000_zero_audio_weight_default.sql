-- audio_weight defaults to 0, not the design spec's 0.3, because the pendant
-- records no audio: firmware/visio_recorder/capture.py runs rpicam-vid with no
-- ALSA source, so every segment is silent video. Scoring a silent track is at
-- best a constant term and at worst noise - Whisper is well documented for
-- hallucinating speech on silence, and if it does so inconsistently across
-- segments that term carries 30% of the composite score at random. Which of the
-- two actually happens is unmeasured, and segments stores only composite_score,
-- so it cannot be reconstructed from past runs. Zeroing removes the risk either
-- way.
--
-- This is deliberate and interim, not a tuning choice. Restore this default to
-- 0.3, together with ScoreWeights.audio_weight in pipeline/pipeline/models.py,
-- when vision-audio-capture lands a microphone. The two defaults must always
-- agree: score_weights rows are optional and the pipeline falls back to the
-- dataclass when a user has none.
--
-- Only the default changes. Existing rows keep whatever weight they hold, since
-- a stored 0.3 is indistinguishable from a weight its owner chose.
alter table public.score_weights
  alter column audio_weight set default 0;
