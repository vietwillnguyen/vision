from datetime import date, datetime
from pathlib import Path

import pytest

from pipeline.models import ScoreWeights
from pipeline.orchestrator import (
    MAX_DLQ_ATTEMPTS,
    DeviceRecord,
    DlqEntry,
    OrchestratorDeps,
    run_nightly,
)
from pipeline.scoring.audio import TranscriptionResult


class FakeStore:
    def __init__(
        self,
        devices,
        segment_keys=None,
        marker_keys=None,
        pending_dlq=None,
        weights=None,
        segment_rows=None,
    ):
        self.devices = devices
        self.segment_keys = segment_keys or {}
        self.marker_keys = marker_keys or {}
        self.pending_dlq = pending_dlq or {}
        self.weights = weights or {}
        self.segment_rows = segment_rows or {}
        self.flagged_segments: list[str] = []
        self.persisted_segments: list[dict] = []
        self.inserted_reels: list[dict] = []
        self.uploaded_reels: list[tuple[str, date, Path]] = []
        self.recorded_dlq: list[tuple[str, DlqEntry]] = []
        self.resolved_dlq: list[tuple[str, str]] = []
        self.escalated_dlq: list[tuple[str, str]] = []

    def list_devices(self):
        return self.devices

    def list_segment_object_keys(self, device_id, day):
        return self.segment_keys.get(device_id, [])

    def list_flag_marker_keys(self, device_id, day):
        return self.marker_keys.get(device_id, [])

    def load_pending_dlq(self, device_id):
        return self.pending_dlq.get(device_id, [])

    def fetch_score_weights(self, user_id):
        return self.weights.get(user_id, ScoreWeights())

    def persist_segments(self, rows):
        self.persisted_segments.extend(rows)

    def download_segment(self, s3_key, workdir):
        local = workdir / Path(s3_key).name
        local.write_bytes(b"video")
        return local

    def upload_reel(self, device_id, day, local_path):
        self.uploaded_reels.append((device_id, day, local_path))
        return f"{device_id}/{day.strftime('%Y%m%d')}.mp4"

    def insert_reel(self, row):
        self.inserted_reels.append(row)

    def record_dlq(self, device_id, entries):
        self.recorded_dlq.extend((device_id, e) for e in entries)

    def resolve_dlq(self, device_id, keys):
        self.resolved_dlq.extend((device_id, k) for k in keys)

    def escalate_dlq(self, device_id, keys):
        self.escalated_dlq.extend((device_id, k) for k in keys)

    def list_segment_rows(self, device_id, day):
        return [
            row
            for row in self.segment_rows.get(device_id, [])
            if row["recorded_at"].startswith(day.isoformat())
        ]

    def flag_segment(self, s3_key):
        self.flagged_segments.append(s3_key)


class FakeMedia:
    def __init__(self, frame_diffs=None):
        self.frame_diffs = frame_diffs if frame_diffs is not None else [200.0]

    def extract_audio(self, video_path, workdir):
        audio = workdir / (video_path.stem + ".wav")
        audio.write_bytes(b"audio")
        return audio

    def extract_frame_diffs(self, video_path):
        return self.frame_diffs

    def extract_frames(self, video_path, workdir):
        frame = workdir / (video_path.stem + ".jpg")
        frame.write_bytes(b"jpg")
        return [frame]


class FakeTranscriber:
    def transcribe(self, audio_path):
        return TranscriptionResult(
            speech_presence_ratio=0.8, silence_ratio=0.1, has_exclamation=False
        )


class FakeVision:
    def __init__(self, location="indoor"):
        self.location = location
        self.calls = 0

    def score_frames(self, frame_paths, prompt):
        self.calls += 1
        return f'{{"score": 8, "location": "{self.location}", "people": true}}'


class FakePush:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to_token, title, body):
        self.sent.append((to_token, title, body))


class FakeRunner:
    def __init__(self):
        self.commands: list[list[str]] = []

    def run(self, command):
        self.commands.append(command)
        output = Path(command[-1])
        output.write_bytes(b"out")


DEVICE = DeviceRecord(device_id="dev-1", user_id="user-1", push_token="ExponentPushToken[x]")
DAY = date(2026, 7, 14)


def make_deps(store, tmp_path, vision=None, media=None, push=None, runner=None):
    return OrchestratorDeps(
        store=store,
        media=media or FakeMedia(),
        transcriber=FakeTranscriber(),
        vision=vision or FakeVision(),
        push=push or FakePush(),
        runner=runner or FakeRunner(),
        workdir=tmp_path,
    )


def segment_key(hhmmss: str) -> str:
    return f"dev-1/20260714_{hhmmss}.mp4"


class TestHappyPath:
    def test_produces_reel_row_and_notification_for_a_device_day(self, tmp_path):
        store = FakeStore(
            devices=[DEVICE],
            segment_keys={"dev-1": [segment_key("090000"), segment_key("100000")]},
        )
        push = FakePush()
        deps = make_deps(store, tmp_path, push=push)

        result = run_nightly(deps, DAY)

        assert len(store.inserted_reels) == 1
        reel = store.inserted_reels[0]
        assert reel["device_id"] == "dev-1"
        assert reel["date"] == "2026-07-14"
        assert reel["s3_key"] == "dev-1/20260714.mp4"
        assert reel["duration_sec"] > 0
        assert len(store.uploaded_reels) == 1
        assert len(push.sent) == 1
        assert push.sent[0][0] == "ExponentPushToken[x]"
        assert result.devices_processed == 1

    def test_persists_scored_segment_rows(self, tmp_path):
        store = FakeStore(
            devices=[DEVICE], segment_keys={"dev-1": [segment_key("090000")]}
        )
        deps = make_deps(store, tmp_path)

        run_nightly(deps, DAY)

        assert len(store.persisted_segments) == 1
        row = store.persisted_segments[0]
        assert row["device_id"] == "dev-1"
        assert row["s3_key"] == segment_key("090000")
        assert row["recorded_at"] == datetime(2026, 7, 14, 9, 0, 0).isoformat()
        assert row["composite_score"] > 0
        assert row["manually_flagged"] is False

    def test_flag_marker_flags_matching_segment(self, tmp_path):
        store = FakeStore(
            devices=[DEVICE],
            segment_keys={"dev-1": [segment_key("090000")]},
            marker_keys={"dev-1": ["dev-1/FLAG_20260714_090100.marker"]},
        )
        deps = make_deps(store, tmp_path)

        run_nightly(deps, DAY)

        assert store.persisted_segments[0]["manually_flagged"] is True

    def test_runs_trim_per_selected_clip_and_one_concat(self, tmp_path):
        store = FakeStore(
            devices=[DEVICE],
            segment_keys={"dev-1": [segment_key("090000"), segment_key("100000")]},
        )
        runner = FakeRunner()
        deps = make_deps(store, tmp_path, runner=runner)

        run_nightly(deps, DAY)

        trim_commands = [c for c in runner.commands if "-ss" in c]
        concat_commands = [c for c in runner.commands if "concat" in c]
        assert len(trim_commands) >= 1
        assert len(concat_commands) == 1

    def test_uses_per_user_score_weights(self, tmp_path):
        heavy_motion = ScoreWeights(scene_weight=0.0, audio_weight=0.0, motion_weight=1.0)
        store = FakeStore(
            devices=[DEVICE],
            segment_keys={"dev-1": [segment_key("090000")]},
            weights={"user-1": heavy_motion},
        )
        deps = make_deps(store, tmp_path, media=FakeMedia(frame_diffs=[255.0]))

        run_nightly(deps, DAY)

        assert store.persisted_segments[0]["composite_score"] == pytest.approx(1.0)


class TestEdgeCases:
    def test_no_segments_means_no_reel_and_no_notification(self, tmp_path):
        store = FakeStore(devices=[DEVICE])
        push = FakePush()
        deps = make_deps(store, tmp_path, push=push)

        result = run_nightly(deps, DAY)

        assert store.inserted_reels == []
        assert push.sent == []
        assert result.devices_processed == 1

    def test_device_without_push_token_still_gets_reel(self, tmp_path):
        tokenless = DeviceRecord(device_id="dev-1", user_id="user-1", push_token=None)
        store = FakeStore(
            devices=[tokenless], segment_keys={"dev-1": [segment_key("090000")]}
        )
        push = FakePush()
        deps = make_deps(store, tmp_path, push=push)

        run_nightly(deps, DAY)

        assert len(store.inserted_reels) == 1
        assert push.sent == []

    def test_one_device_failure_does_not_block_other_devices(self, tmp_path):
        class ExplodingStore(FakeStore):
            def list_segment_object_keys(self, device_id, day):
                if device_id == "dev-1":
                    raise RuntimeError("storage down")
                return super().list_segment_object_keys(device_id, day)

        other = DeviceRecord(device_id="dev-2", user_id="user-2", push_token=None)
        store = ExplodingStore(
            devices=[DEVICE, other],
            segment_keys={"dev-2": ["dev-2/20260714_090000.mp4"]},
        )
        deps = make_deps(store, tmp_path)

        result = run_nightly(deps, DAY)

        assert len(store.inserted_reels) == 1
        assert store.inserted_reels[0]["device_id"] == "dev-2"
        assert result.devices_processed == 1
        assert result.devices_failed == ["dev-1"]


class TestDlqPolicy:
    def test_new_rejected_and_unmatched_keys_are_recorded_with_one_attempt(
        self, tmp_path
    ):
        store = FakeStore(
            devices=[DEVICE],
            segment_keys={"dev-1": [segment_key("090000"), "dev-1/garbage.mp4"]},
            marker_keys={"dev-1": ["dev-1/FLAG_20260714_235959.marker"]},
        )
        deps = make_deps(store, tmp_path)

        run_nightly(deps, DAY)

        recorded = {e.key: e for _, e in store.recorded_dlq}
        assert recorded["dev-1/garbage.mp4"].kind == "rejected"
        assert recorded["dev-1/garbage.mp4"].attempts == 1
        assert recorded["dev-1/FLAG_20260714_235959.marker"].kind == "unmatched"
        assert recorded["dev-1/FLAG_20260714_235959.marker"].attempts == 1

    def test_pending_unmatched_marker_that_now_matches_is_resolved(self, tmp_path):
        marker = "dev-1/FLAG_20260714_090100.marker"
        store = FakeStore(
            devices=[DEVICE],
            segment_keys={"dev-1": [segment_key("090000")]},
            pending_dlq={"dev-1": [DlqEntry(key=marker, kind="unmatched", attempts=2)]},
        )
        deps = make_deps(store, tmp_path)

        run_nightly(deps, DAY)

        assert ("dev-1", marker) in store.resolved_dlq
        assert store.persisted_segments[0]["manually_flagged"] is True

    def test_still_failing_pending_key_increments_attempts(self, tmp_path):
        marker = "dev-1/FLAG_20260713_090100.marker"
        store = FakeStore(
            devices=[DEVICE],
            segment_keys={"dev-1": [segment_key("090000")]},
            pending_dlq={"dev-1": [DlqEntry(key=marker, kind="unmatched", attempts=2)]},
        )
        deps = make_deps(store, tmp_path)

        run_nightly(deps, DAY)

        recorded = {e.key: e for _, e in store.recorded_dlq}
        assert recorded[marker].attempts == 3

    def test_key_reaching_max_attempts_is_escalated_and_user_is_notified(
        self, tmp_path
    ):
        marker = "dev-1/FLAG_20260713_090100.marker"
        store = FakeStore(
            devices=[DEVICE],
            segment_keys={"dev-1": [segment_key("090000")]},
            pending_dlq={
                "dev-1": [
                    DlqEntry(key=marker, kind="unmatched", attempts=MAX_DLQ_ATTEMPTS - 1)
                ]
            },
        )
        push = FakePush()
        deps = make_deps(store, tmp_path, push=push)

        run_nightly(deps, DAY)

        assert ("dev-1", marker) in store.escalated_dlq
        escalation_messages = [body for _, _, body in push.sent if "processed" in body]
        assert len(escalation_messages) == 1

    def test_recovered_marker_is_not_resolved_when_processing_later_fails(
        self, tmp_path
    ):
        class FailingPersistStore(FakeStore):
            def persist_segments(self, rows):
                raise RuntimeError("db down")

        marker = "dev-1/FLAG_20260714_090100.marker"
        store = FailingPersistStore(
            devices=[DEVICE],
            segment_keys={"dev-1": [segment_key("090000")]},
            pending_dlq={"dev-1": [DlqEntry(key=marker, kind="unmatched", attempts=2)]},
        )
        deps = make_deps(store, tmp_path)

        result = run_nightly(deps, DAY)

        assert result.devices_failed == ["dev-1"]
        assert store.resolved_dlq == []

    def test_retried_marker_matches_persisted_segment_of_its_own_day(self, tmp_path):
        marker = "dev-1/FLAG_20260713_090100.marker"
        store = FakeStore(
            devices=[DEVICE],
            pending_dlq={
                "dev-1": [
                    DlqEntry(key=marker, kind="unmatched", attempts=MAX_DLQ_ATTEMPTS - 1)
                ]
            },
            segment_rows={
                "dev-1": [
                    {
                        "s3_key": "dev-1/20260713_090000.mp4",
                        "recorded_at": "2026-07-13T09:00:00",
                        "duration_sec": 300,
                    }
                ]
            },
        )
        push = FakePush()
        deps = make_deps(store, tmp_path, push=push)

        run_nightly(deps, DAY)

        assert store.flagged_segments == ["dev-1/20260713_090000.mp4"]
        assert ("dev-1", marker) in store.resolved_dlq
        assert store.recorded_dlq == []
        assert store.escalated_dlq == []
        assert push.sent == []

    def test_retried_marker_without_persisted_match_keeps_retrying(self, tmp_path):
        marker = "dev-1/FLAG_20260713_235900.marker"
        store = FakeStore(
            devices=[DEVICE],
            pending_dlq={"dev-1": [DlqEntry(key=marker, kind="unmatched", attempts=1)]},
            segment_rows={
                "dev-1": [
                    {
                        "s3_key": "dev-1/20260713_090000.mp4",
                        "recorded_at": "2026-07-13T09:00:00",
                        "duration_sec": 300,
                    }
                ]
            },
        )
        deps = make_deps(store, tmp_path)

        run_nightly(deps, DAY)

        assert store.flagged_segments == []
        recorded = {e.key: e for _, e in store.recorded_dlq}
        assert recorded[marker].attempts == 2
