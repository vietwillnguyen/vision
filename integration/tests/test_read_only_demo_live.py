"""The public demo, end to end against a live local Supabase.

Seeds a demo account with ``pipeline.demo`` exactly as the runbook does (tiny
placeholder clips, stub scoring, the real nightly pipeline), then signs in as
that account with the anon key - what the public web build does - and drives
every write a client could attempt through the real REST, Storage and Auth
APIs. pgTAP (``supabase/tests/database/read_only_users.test.sql``) proves each
policy decision in SQL; this proves the HTTP surfaces a stranger can reach
actually end at those policies.

Needs ffmpeg and a running ``supabase start``; skips otherwise.
"""

import shutil
import uuid
from collections.abc import Iterator
from datetime import date, time
from typing import Any

import pytest
from postgrest.exceptions import APIError
from storage3.exceptions import StorageApiError
from supabase import create_client
from supabase_auth.errors import AuthApiError

from pipeline.adapters.ffmpeg_media import SubprocessRunner
from pipeline.demo.footage import load_source_clips, plan_day, render_day
from pipeline.demo.placeholder import PlaceholderClip, generate_placeholder_clips
from pipeline.demo.scoring import StubTranscriber, StubVision
from pipeline.demo.seed import NotReadOnlyError, remove_demo_account, seed_demo

pytestmark = [
    pytest.mark.usefixtures("live_supabase_env"),
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg"),
]

DAY = date(2026, 11, 20)
PASSWORD = "read-only-demo-test"
CLIPS = (
    PlaceholderClip("01.mp4", 3, "testsrc2", flags=(1,)),
    PlaceholderClip("02.mp4", 2, "bars", sound=True),
)


@pytest.fixture(scope="module")
def seeded(live_supabase_env, tmp_path_factory) -> Iterator[dict[str, Any]]:
    tmp = tmp_path_factory.mktemp("demo")
    admin = create_client(
        live_supabase_env["url"], live_supabase_env["service_role_key"]
    )
    email = f"demo-{uuid.uuid4().hex[:8]}@example.test"
    sources, manifest = load_source_clips(
        generate_placeholder_clips(SubprocessRunner(), tmp / "clips", CLIPS)
    )
    plan = plan_day(sources, DAY, default_start=manifest.start or time(9, 0))
    result = seed_demo(
        admin,
        email=email,
        password=PASSWORD,
        day=DAY,
        plan=plan,
        segment_paths=render_day(SubprocessRunner(), plan, tmp / "segments"),
        transcriber=StubTranscriber(),
        vision=StubVision(),
        workdir=tmp / "work",
    )
    yield {"admin": admin, "email": email, "result": result, **live_supabase_env}
    remove_demo_account(admin, email)


def demo_client(seeded: dict[str, Any]):
    client = create_client(seeded["url"], seeded["anon_key"])
    client.auth.sign_in_with_password({"email": seeded["email"], "password": PASSWORD})
    return client


def test_the_seed_builds_a_real_reel_for_a_read_only_account(seeded):
    result = seeded["result"]
    user = seeded["admin"].auth.admin.get_user_by_id(result.user_id).user

    assert user.app_metadata.get("read_only") is True
    assert result.segments == 2
    assert result.flagged_segments == 1
    assert result.reel_duration_sec > 0
    assert result.reel_key == f"{result.device_id}/20261120.mp4"


def test_the_demo_account_reads_its_device_day_and_can_play_it(seeded):
    client = demo_client(seeded)
    device_id = seeded["result"].device_id

    devices = client.table("devices").select("device_id, name").execute().data
    assert [d["device_id"] for d in devices] == [device_id]
    assert len(client.table("segments").select("id").execute().data) == 2
    assert len(client.table("reels").select("id").execute().data) == 1
    assert client.table("device_status").select("battery_pct").execute().data
    signed = client.storage.from_("reels").create_signed_url(
        seeded["result"].reel_key, 60
    )
    assert signed["signedURL"]


def test_the_demo_account_cannot_write_any_table(seeded):
    client = demo_client(seeded)
    admin = seeded["admin"]
    result = seeded["result"]
    segment_id = client.table("segments").select("id").limit(1).execute().data[0]["id"]

    with pytest.raises(APIError):
        client.table("devices").insert(
            {"user_id": result.user_id, "name": "x"}
        ).execute()
    with pytest.raises(APIError):
        client.table("device_status").upsert(
            {"device_id": result.device_id, "battery_pct": 1}
        ).execute()
    with pytest.raises(APIError):
        client.table("score_weights").insert({"user_id": result.user_id}).execute()
    # Updates and deletes find no row they may touch and change nothing.
    assert (
        client.table("devices")
        .update({"name": "defaced"})
        .eq("device_id", result.device_id)
        .execute()
        .data
        == []
    )
    assert (
        client.table("segments")
        .update({"user_feedback": "exclude"})
        .eq("id", segment_id)
        .execute()
        .data
        == []
    )
    assert (
        client.table("devices")
        .delete()
        .eq("device_id", result.device_id)
        .execute()
        .data
        == []
    )

    device = admin.table("devices").select("name").eq("device_id", result.device_id)
    assert device.execute().data[0]["name"] == "Visio bench prototype"
    feedback = admin.table("segments").select("user_feedback").eq("id", segment_id)
    assert feedback.execute().data[0]["user_feedback"] is None


def test_the_demo_account_cannot_change_storage(seeded):
    client = demo_client(seeded)
    result = seeded["result"]
    segments = client.storage.from_("segments")
    reel = result.reel_key

    with pytest.raises(StorageApiError):
        segments.upload(f"{result.device_id}/20261120_120000.mp4", b"x")
    with pytest.raises(StorageApiError):
        segments.upload(f"{result.device_id}/FLAG_20261120_120000.marker", b"")
    with pytest.raises(StorageApiError):
        client.storage.from_("reels").update(reel, b"overwritten")
    with pytest.raises(StorageApiError):
        client.storage.from_("reels").move(reel, f"{result.device_id}/moved.mp4")
    assert client.storage.from_("reels").remove([reel]) == []

    still_there = seeded["admin"].storage.from_("reels").download(reel)
    assert len(still_there) > 1000


def test_the_demo_account_cannot_change_its_own_login(seeded):
    client = demo_client(seeded)

    with pytest.raises(AuthApiError):
        client.auth.update_user({"password": "taken-over"})
    with pytest.raises(AuthApiError):
        client.auth.update_user({"email": "attacker@example.test"})
    with pytest.raises(AuthApiError):
        client.auth.update_user({"data": {"name": "defaced"}})

    # The next visitor still gets in with the published password.
    assert demo_client(seeded).auth.get_session() is not None


def test_the_seed_never_deletes_an_account_that_is_not_read_only(live_supabase_env):
    admin = create_client(
        live_supabase_env["url"], live_supabase_env["service_role_key"]
    )
    email = f"owner-{uuid.uuid4().hex[:8]}@example.test"
    user = admin.auth.admin.create_user(
        {"email": email, "password": PASSWORD, "email_confirm": True}
    ).user
    try:
        with pytest.raises(NotReadOnlyError):
            remove_demo_account(admin, email)
        assert admin.auth.admin.get_user_by_id(user.id).user.email == email
    finally:
        admin.auth.admin.delete_user(user.id)
