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

import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from supabase import create_client

from pipeline.adapters.supabase_store import SupabaseStore
from pipeline.ingestion import build_segments_from_object_keys
from visio_recorder.supabase_clients import SupabaseStorage
from visio_recorder.uploader import upload_segment

pytestmark = pytest.mark.usefixtures("live_supabase_env")


def test_firmware_uploaded_object_is_ingested_by_pipeline_listing(
    live_supabase_env, tmp_path: Path
):
    url = live_supabase_env["url"]
    admin_client = create_client(url, live_supabase_env["service_role_key"])
    run_id = uuid.uuid4().hex[:8]
    owner_email = f"storage-owner-{run_id}@example.test"
    password = "correct-horse-battery-staple"

    owner_id = admin_client.auth.admin.create_user(
        {"email": owner_email, "password": password, "email_confirm": True}
    ).user.id

    device_id = None
    stub_segment = None
    try:
        device = (
            admin_client.table("devices")
            .insert({"user_id": owner_id, "name": f"storage test device {run_id}"})
            .execute()
            .data[0]
        )
        device_id = device["device_id"]

        recorded_at = datetime(2026, 7, 14, 9, 0, 0, tzinfo=UTC)
        stub_segment = tmp_path / recorded_at.strftime("%Y%m%d_%H%M%S.mp4")
        stub_segment.write_bytes(b"stub mp4 bytes")

        owner_client = create_client(url, live_supabase_env["anon_key"])
        owner_client.auth.sign_in_with_password(
            {"email": owner_email, "password": password}
        )
        object_path = upload_segment(
            SupabaseStorage(owner_client), device_id, stub_segment
        )
        assert object_path == f"{device_id}/{stub_segment.name}"

        pipeline_store = SupabaseStore(admin_client)
        object_keys = pipeline_store.list_segment_object_keys(
            device_id, date(2026, 7, 14)
        )
        assert object_keys == [object_path], (
            "pipeline's live bucket listing must see the object firmware's "
            "real upload adapter just wrote"
        )

        segments, rejected_keys = build_segments_from_object_keys(
            object_keys, device_id
        )
        assert rejected_keys == []
        assert len(segments) == 1
        assert segments[0].device_id == device_id
        assert segments[0].recorded_at == recorded_at.replace(tzinfo=None)
        assert segments[0].s3_key == object_path
    finally:
        if device_id is not None and stub_segment is not None:
            admin_client.storage.from_("segments").remove(
                [f"{device_id}/{stub_segment.name}"]
            )
        admin_client.auth.admin.delete_user(owner_id)
