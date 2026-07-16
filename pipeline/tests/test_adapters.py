from pathlib import Path

import pytest

from pipeline.adapters.expo_push import (
    EXPO_PUSH_URL,
    ExpoPushClient,
    ExpoPushError,
    build_expo_payload,
)
from pipeline.adapters.ffmpeg_media import (
    FRAME_HEIGHT,
    FRAME_WIDTH,
    build_audio_extract_command,
    build_frame_extract_command,
    build_raw_gray_command,
    compute_frame_diffs_from_raw,
    run_captured,
)
from pipeline.adapters.litellm_clients import (
    build_vision_request,
    transcription_result_from_verbose,
)
from pipeline.config import Config, load_config
from pipeline.delivery.notifier import PushTokenNotRegisteredError


class TestFrameDiffs:
    def test_identical_frames_produce_zero_diff(self):
        frame = bytes([100] * 4)
        diffs = compute_frame_diffs_from_raw(frame + frame, frame_size=4)

        assert diffs == [0.0]

    def test_mean_absolute_difference_per_frame_pair(self):
        first = bytes([0, 0, 0, 0])
        second = bytes([255, 255, 255, 255])
        third = bytes([255, 245, 255, 245])

        diffs = compute_frame_diffs_from_raw(first + second + third, frame_size=4)

        assert diffs == [255.0, 5.0]

    def test_fewer_than_two_frames_gives_no_diffs(self):
        assert compute_frame_diffs_from_raw(bytes([1, 2, 3]), frame_size=4) == []

    def test_trailing_partial_frame_is_ignored(self):
        first = bytes([0, 0, 0, 0])
        second = bytes([10, 10, 10, 10])
        partial = bytes([99])

        diffs = compute_frame_diffs_from_raw(first + second + partial, frame_size=4)

        assert diffs == [10.0]


class TestFfmpegCommands:
    def test_raw_gray_command_downscales_to_gray_rawvideo(self):
        cmd = build_raw_gray_command(Path("in.mp4"))

        assert cmd[0] == "ffmpeg"
        assert "rawvideo" in cmd
        assert "gray" in cmd
        assert f"scale={FRAME_WIDTH}:{FRAME_HEIGHT}" in " ".join(cmd)
        assert cmd[-1] == "-"

    def test_frame_extract_command_writes_jpegs_to_pattern(self):
        cmd = build_frame_extract_command(Path("in.mp4"), Path("/tmp/frames"))

        assert cmd[0] == "ffmpeg"
        assert str(Path("/tmp/frames") / "frame_%03d.jpg") in cmd

    def test_audio_extract_command_produces_wav(self):
        cmd = build_audio_extract_command(Path("in.mp4"), Path("/tmp/a.wav"))

        assert cmd[0] == "ffmpeg"
        assert cmd[-1] == "/tmp/a.wav"


class TestVisionRequest:
    def test_request_uses_json_schema_structured_output(self, tmp_path):
        frame = tmp_path / "f.jpg"
        frame.write_bytes(b"\xff\xd8fake")

        request = build_vision_request("claude-haiku", [frame], "rate this")

        assert request["model"] == "claude-haiku"
        response_format = request["response_format"]
        assert response_format["type"] == "json_schema"
        schema = response_format["json_schema"]["schema"]
        assert set(schema["required"]) == {"score", "location", "people"}
        assert schema["properties"]["location"]["enum"] == ["indoor", "outdoor"]

    def test_request_embeds_frames_as_base64_image_parts(self, tmp_path):
        frame = tmp_path / "f.jpg"
        frame.write_bytes(b"\xff\xd8fake")

        request = build_vision_request("claude-haiku", [frame], "rate this")

        content = request["messages"][0]["content"]
        image_parts = [p for p in content if p["type"] == "image_url"]
        text_parts = [p for p in content if p["type"] == "text"]
        assert len(image_parts) == 1
        assert image_parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert text_parts[0]["text"] == "rate this"


class TestTranscriptionMapping:
    def test_speech_and_silence_ratios_from_verbose_segments(self):
        verbose = {
            "duration": 10.0,
            "text": "hello there",
            "segments": [
                {"start": 0.0, "end": 4.0},
                {"start": 6.0, "end": 8.0},
            ],
        }

        result = transcription_result_from_verbose(verbose)

        assert result.speech_presence_ratio == pytest.approx(0.6)
        assert result.silence_ratio == pytest.approx(0.4)
        assert result.has_exclamation is False

    def test_exclamation_detected_from_text(self):
        verbose = {"duration": 5.0, "text": "wow!", "segments": [{"start": 0, "end": 5}]}

        assert transcription_result_from_verbose(verbose).has_exclamation is True

    def test_zero_duration_yields_silent_result(self):
        result = transcription_result_from_verbose(
            {"duration": 0, "text": "", "segments": []}
        )

        assert result.speech_presence_ratio == 0.0
        assert result.silence_ratio == 1.0


class TestExpoPush:
    def test_payload_shape(self):
        payload = build_expo_payload("ExponentPushToken[x]", "title", "body")

        assert payload == {
            "to": "ExponentPushToken[x]",
            "title": "title",
            "body": "body",
        }

    def test_client_posts_payload_to_expo_endpoint(self):
        calls = []

        def fake_post(url, json_body):
            calls.append((url, json_body))

        client = ExpoPushClient(post=fake_post)
        client.send("ExponentPushToken[x]", "title", "body")

        assert calls == [
            (EXPO_PUSH_URL, build_expo_payload("ExponentPushToken[x]", "title", "body"))
        ]

    def test_ticket_error_in_200_response_raises(self):
        def fake_post(url, json_body):
            return {
                "data": [
                    {
                        "status": "error",
                        "message": "not a registered push notification recipient",
                        "details": {"error": "DeviceNotRegistered"},
                    }
                ]
            }

        client = ExpoPushClient(post=fake_post)

        with pytest.raises(ExpoPushError, match="DeviceNotRegistered"):
            client.send("ExponentPushToken[x]", "title", "body")

    def test_device_not_registered_raises_token_not_registered_error(self):
        client = ExpoPushClient(
            post=lambda url, body: {
                "data": [{"status": "error", "details": {"error": "DeviceNotRegistered"}}]
            }
        )

        with pytest.raises(PushTokenNotRegisteredError):
            client.send("ExponentPushToken[x]", "title", "body")

    def test_single_ticket_dict_error_raises(self):
        client = ExpoPushClient(
            post=lambda url, body: {
                "data": {"status": "error", "details": {"error": "MessageRateExceeded"}}
            }
        )

        with pytest.raises(ExpoPushError, match="MessageRateExceeded"):
            client.send("ExponentPushToken[x]", "title", "body")

    def test_ok_ticket_does_not_raise(self):
        client = ExpoPushClient(
            post=lambda url, body: {"data": [{"status": "ok", "id": "ticket-1"}]}
        )

        client.send("ExponentPushToken[x]", "title", "body")


class TestRunCaptured:
    def test_failure_surfaces_stderr_in_error_message(self):
        command = [
            "python3",
            "-c",
            "import sys; sys.stderr.write('boom: codec not found'); sys.exit(2)",
        ]

        with pytest.raises(RuntimeError, match="codec not found"):
            run_captured(command)

    def test_success_returns_completed_process_with_stdout(self):
        completed = run_captured(["python3", "-c", "print('ok')"])

        assert completed.stdout.strip() == b"ok"


class TestConfig:
    def test_loads_required_and_default_values(self):
        env = {
            "SUPABASE_URL": "https://x.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-key",
        }

        config = load_config(env)

        assert config == Config(
            supabase_url="https://x.supabase.co",
            supabase_service_role_key="service-key",
            vision_model="anthropic/claude-haiku-4-5-20251001",
            transcription_model="whisper-1",
            target_duration_sec=90,
            vintage=False,
        )

    def test_overrides_from_env(self):
        env = {
            "SUPABASE_URL": "https://x.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-key",
            "VISIO_VISION_MODEL": "gpt-4o-mini",
            "VISIO_TRANSCRIPTION_MODEL": "groq/whisper-large-v3",
            "VISIO_TARGET_DURATION_SEC": "120",
            "VISIO_VINTAGE": "1",
        }

        config = load_config(env)

        assert config.vision_model == "gpt-4o-mini"
        assert config.transcription_model == "groq/whisper-large-v3"
        assert config.target_duration_sec == 120
        assert config.vintage is True

    def test_missing_required_variable_raises_with_its_name(self):
        with pytest.raises(ValueError, match="SUPABASE_SERVICE_ROLE_KEY"):
            load_config({"SUPABASE_URL": "https://x.supabase.co"})
