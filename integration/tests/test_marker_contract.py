"""Cross-service filename contract tests.

Stage 0 of the test-validation-plan integration suite (see
.lavish/test-validation-plan.html): firmware and pipeline each have their own
unit tests for filename handling, but nothing today asserts that firmware's
*writer* and pipeline's *parser* agree on the wire format. These tests import
both real packages directly (no mocks, no Supabase instance needed) and
round-trip a value through both sides.
"""

from datetime import datetime
from pathlib import Path

from pipeline.ingestion import parse_flag_marker_filename, parse_segment_filename
from visio_recorder.capture import segment_filename
from visio_recorder.flag_button import write_flag_marker
from visio_recorder.muxer import mux_segment
from visio_recorder.uploader import upload_segment


def test_segment_filename_round_trips_through_pipeline_parser():
    """capture.segment_filename() names the raw .h264 capture; muxer.mux_segment()
    is what actually produces the .mp4 that gets uploaded and that pipeline parses.
    """
    started_at = datetime(2026, 7, 19, 22, 15, 7)
    h264_path = Path(segment_filename(started_at))

    class NoopRunner:
        def run(self, args):
            pass

    mp4_path = mux_segment(NoopRunner(), h264_path, framerate=30)

    assert parse_segment_filename(mp4_path.name) == started_at


def test_flag_marker_round_trips_through_pipeline_parser(tmp_path):
    pressed_at = datetime(2026, 7, 19, 22, 17, 42)

    marker_path = write_flag_marker(tmp_path, pressed_at)

    assert parse_flag_marker_filename(marker_path.name) == pressed_at


def test_uploaded_segment_object_key_matches_pipeline_device_prefix_convention():
    class RecordingStorageClient:
        def __init__(self):
            self.calls = []

        def upload(self, bucket, object_path, local_path):
            self.calls.append((bucket, object_path, local_path))

    started_at = datetime(2026, 7, 19, 22, 15, 7)

    class NoopRunner:
        def run(self, args):
            pass

    local_path = mux_segment(NoopRunner(), Path(segment_filename(started_at)), framerate=30)
    client = RecordingStorageClient()

    object_key = upload_segment(client, "device-123", local_path)

    assert client.calls == [("segments", object_key, local_path)]
    assert object_key.startswith("device-123/")
    assert parse_segment_filename(object_key.split("/")[-1]) == started_at
