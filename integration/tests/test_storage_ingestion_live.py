"""Live-Supabase storage handoff test (Stage 1 -> Stage 2 of the
test-validation-plan, see .lavish/test-validation-plan.html).

Prior iterations' RLS test (test_rls_end_to_end.py) drives pipeline's
SupabaseStore.persist_segments() directly to prove RLS gates DB rows, but
that sidesteps how a `segments` row actually comes to exist in production:
firmware never inserts one. Firmware only uploads a stub-named MP4 object to
the `segments` storage bucket (visio_recorder.uploader.upload_segment); it is
pipeline that lists that bucket (SupabaseStore.list_segment_object_keys) and
parses the real object keys into segment rows
(pipeline.ingestion.build_segments_from_object_keys). Nothing before this
test exercised firmware's real upload adapter against a live bucket feeding
pipeline's real listing/parsing - the actual Stage 1 -> Stage 2 contract.

Needs a running `supabase start` instance; skips automatically otherwise
(see conftest.py and integration/README.md).
"""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from pipeline.adapters.supabase_store import SupabaseStore
from pipeline.ingestion import build_segments_from_object_keys
from tests.pendant import PairedDevice
from visio_recorder.uploader import upload_segment

pytestmark = pytest.mark.usefixtures("live_supabase_env")


def test_firmware_uploaded_object_is_ingested_by_pipeline_listing(
    paired_device: PairedDevice, tmp_path: Path
):
    admin_client = paired_device.admin
    device_id = paired_device.device_id

    recorded_at = datetime(2026, 7, 14, 9, 0, 0, tzinfo=UTC)
    stub_segment = tmp_path / recorded_at.strftime("%Y%m%d_%H%M%S.mp4")
    stub_segment.write_bytes(b"stub mp4 bytes")

    # Uploaded as the paired pendant's own login, the only one that may write
    # to the segments bucket; the paired_device fixture removes the object.
    object_path = upload_segment(
        paired_device.pendant.clients.storage, device_id, stub_segment
    )
    assert object_path == f"{device_id}/{stub_segment.name}"

    pipeline_store = SupabaseStore(admin_client)
    object_keys = pipeline_store.list_segment_object_keys(device_id, date(2026, 7, 14))
    assert object_keys == [object_path], (
        "pipeline's live bucket listing must see the object firmware's "
        "real upload adapter just wrote"
    )

    # The real probe is ffmpeg (covered in pipeline's
    # test_ffmpeg_media_live.py); this test is about the storage handoff,
    # so it measures the bytes pipeline actually downloads from the bucket.
    downloaded: dict[str, bytes] = {}

    def measure_downloaded(key: str) -> float:
        local = pipeline_store.download_segment(key, tmp_path / "pipeline")
        downloaded[key] = local.read_bytes()
        return 40.0

    (tmp_path / "pipeline").mkdir()
    segments, rejected_keys = build_segments_from_object_keys(
        object_keys, device_id, measure_downloaded
    )
    assert rejected_keys == []
    assert len(segments) == 1
    assert segments[0].device_id == device_id
    assert segments[0].recorded_at == recorded_at.replace(tzinfo=None)
    assert segments[0].s3_key == object_path
    assert segments[0].duration_sec == 40.0
    assert downloaded == {object_path: b"stub mp4 bytes"}
