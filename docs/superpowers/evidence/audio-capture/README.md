# Audio capture - end-to-end evidence

No microphone is wired yet, so everything here runs off-device against a local `supabase start` stack with this branch's migrations.

## 00 - before: a reel mixing audio and video-only clips

[`00-before-mixed-reel.log`](00-before-mixed-reel.log) ([`mixed_reel_before.sh`](mixed_reel_before.sh)) runs the pipeline's pre-fix trim and concat commands on three clips, the middle one video only, as on a day the microphone dropped out.

- Clips in order audio, video only, audio: the reel's audio has a 15 s hole (packets jump from 15.0 s to 30.0 s), which players close up, so the third clip's sound plays over the second clip's pictures.
- Clips in order video only, audio, audio: the reel has no audio stream at all, because the concat demuxer takes its stream layout from the first clip.

Before this branch the firmware recorded no audio, so this could not happen; with a microphone that can fail mid-day it can, so it was fixed here (every clip gets an audio track when any has one).

## 01 - after: three pendants, through the real firmware and pipeline

[`01-after-audio-day.log`](01-after-audio-day.log) ([`e2e_audio_day.py`](e2e_audio_day.py)).
Firmware is its own code: `load_config` defaults, `resolve_audio` (a real `ffmpeg -f alsa` open of `visio_mic` through the host's ALSA library), `run_recording_loop`, the ffmpeg remux, the upload guard, and the real storage and status adapters signed in as the owner.
The camera is [`../segment-upload-limit/sim-bin/rpicam-vid`](../segment-upload-limit/sim-bin/rpicam-vid): for `--codec libav --libav-format mpegts --libav-audio --audio-source alsa` it first opens the ALSA device like the real binary (and fails when it cannot), then writes MPEG-TS with CBR H.264 plus mono 48 kHz AAC at the requested bitrate, whose samples are a synthetic speech track (ffmpeg's `flite` voice, three sentences with pauses).
The host's ALSA `file` plugin cannot stream a capture file at real time, so the device open is real and the samples are injected.

| Pendant | ALSA setup | Startup probe | Segments | `audio_active` |
|---|---|---|---|---|
| `with-mic` | a `visio_mic` PCM that opens | microphone kept | 4 x 30.25 MB, H.264 1280x720 + AAC 48 kHz mono | true |
| `mic-drops-out` | same, microphone removed after 2 captures | microphone kept | 2 with audio, then the capture fails and the loop logs it and carries on: 2 video only | false after the drop |
| `no-mic` | the repo's real [`firmware/alsa/visio-mic.conf`](../../../../firmware/alsa/visio-mic.conf), with no `adau7002` card | warning, video only | 4 x 29.98 MB, H.264 only | false |

Nothing was left in any upload queue.
(The failed capture costs one clock read in this harness, whose clock steps a whole segment per read; on a device the next capture starts straight away.)

Nightly pipeline (`run_nightly`, real `SupabaseStore`, ffmpeg media probe and runner, real `LiteLLMTranscriber("whisper-1")`): 3 devices processed, 0 failed.

- **Transcription runs on real audio.** No OpenAI key is configured here, so LiteLLM was pointed through `OPENAI_API_BASE` at [`local_whisper_server.py`](local_whisper_server.py), which answers OpenAI's transcription API with a real Whisper model (faster-whisper `base.en`). The 6 segments with audio were transcribed; [`01-local-whisper-server.log`](01-local-whisper-server.log) has the transcripts ("Good morning, we are at the market buying fresh mangoes. Look at that the boat is full of flowers. ..."). The 6 video-only segments made no transcription call.
- **Audio is scored.** The vision stub scores by time of day, identically for every pendant, so the audio term is the only difference: `with-mic` segments score 0.5612 / 0.6012 / 0.6412 / 0.5611 against `no-mic`'s 0.2614 / 0.3013 / 0.3413 / 0.2613, the 0.3 x 1.0 audio activity at the restored weight.
- **Reels.** `with-mic`: 60 s, H.264 + AAC, audio packets from 0 to 60.0 s with no gap over 0.021 s. `mic-drops-out`: 45 s mixing audio and video-only clips, audio from 0 to 45.0 s with no gap over 0.021 s (the case that lost or shifted audio in 00). `no-mic`: 60 s, video only, as before this branch.

The vision LLM is stubbed because no Anthropic key is configured here.

## Reproducing

```sh
# a Python env with faster-whisper (and av<16), then:
python local_whisper_server.py --port 8765 &
cd integration
eval "$(supabase status -o env)"
export SUPABASE_URL=$API_URL SUPABASE_ANON_KEY=$ANON_KEY SUPABASE_SERVICE_ROLE_KEY=$SERVICE_ROLE_KEY
export OPENAI_API_BASE=http://127.0.0.1:8765/v1 OPENAI_API_KEY=unused
uv run --locked --extra dev python ../docs/superpowers/evidence/audio-capture/e2e_audio_day.py --out /tmp/audio-day
```
