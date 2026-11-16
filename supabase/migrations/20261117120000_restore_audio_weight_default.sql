-- Restore the design spec's audio_weight default of 0.3, zeroed by
-- 20260807120000_zero_audio_weight_default.sql while the pendant recorded no
-- audio. The firmware now records an I2S microphone track with the video
-- (rpicam-vid --codec libav with an ALSA source), and falls back to video only
-- when no microphone opens. A segment without an audio stream is scored as
-- silence without a transcription call (audio_activity 0), so the weight only
-- ever counts for segments that carry real audio.
--
-- Must agree with ScoreWeights.audio_weight in pipeline/pipeline/models.py:
-- score_weights rows are optional and the pipeline falls back to the
-- dataclass for a user without one. As in the zeroing migration, existing rows
-- keep whatever weight they hold; nothing in the app writes this table today,
-- so a stored value is one someone chose.
alter table public.score_weights
  alter column audio_weight set default 0.3;
