"""Seed the read-only demo account with a device day and its real reel.

The account is recreated from scratch on every run: the previous demo user is
deleted with everything under it (its rows cascade; its storage objects are
removed first, since storage has no foreign keys), then a new one is created
with ``app_metadata.read_only = true``, which the database enforces (see
``supabase/migrations/20261121120000_read_only_accounts.sql``). A user with
the demo email that is *not* read-only is never touched.

The footage is uploaded the way the pendant uploads it and the reel is built
by the real ``run_nightly`` with the real Supabase store and FFmpeg; only the
scoring clients are swappable, and the push client never sends.
"""

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

from pipeline.adapters.ffmpeg_media import FfmpegMediaProbe, SubprocessRunner
from pipeline.adapters.supabase_store import (
    REELS_BUCKET,
    SEGMENTS_BUCKET,
    STORAGE_LIST_PAGE_SIZE,
    SupabaseStore,
)
from pipeline.demo.footage import DayPlan
from pipeline.orchestrator import DeviceRecord, OrchestratorDeps, run_nightly
from pipeline.scoring.audio import TranscriptionClient
from pipeline.scoring.scene import VisionClient

logger = logging.getLogger(__name__)

DEFAULT_DEMO_EMAIL = "demo@vision.vnlabs.dev"
DEFAULT_DEVICE_NAME = "Visio bench prototype"
# What the Device tab shows. The prototype runs on USB power with no battery
# meter fitted (PRD J9), so the battery reads full.
DEMO_DEVICE_STATUS = {
    "battery_pct": 100,
    "storage_used_gb": 3.2,
    "storage_free_gb": 24.6,
    "segments_pending": 0,
    "recording_active": False,
}


class OneDeviceStore(SupabaseStore):
    """Restricts a run to one device, so other devices on the same project
    (other local runs, or a real pendant) are never processed alongside it."""

    def __init__(self, client: Any, device_id: str) -> None:
        super().__init__(client)
        self._device_id = device_id

    def list_devices(self) -> list[DeviceRecord]:
        return [d for d in super().list_devices() if d.device_id == self._device_id]


class RecordingPush:
    """Records instead of sending; a seed or harness never notifies anyone."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to_token: str, title: str, body: str) -> None:
        self.sent.append((to_token, title, body))


class NotReadOnlyError(RuntimeError):
    pass


@dataclass(frozen=True)
class SeedResult:
    user_id: str
    device_id: str
    day: date
    segments: int
    flagged_segments: int
    reel_key: str
    reel_duration_sec: int


def find_user(client: Any, email: str) -> tuple[str, dict] | None:
    """The user's id and app_metadata, or None; GoTrue has no lookup by email."""
    page = 1
    while True:
        users = client.auth.admin.list_users(page=page, per_page=100)
        for user in users:
            if (user.email or "").lower() == email.lower():
                return str(user.id), dict(user.app_metadata or {})
        if len(users) < 100:
            return None
        page += 1


def remove_demo_account(client: Any, email: str) -> bool:
    """Delete the read-only account with this email and its storage objects."""
    found = find_user(client, email)
    if found is None:
        return False
    user_id, app_metadata = found
    if app_metadata.get("read_only") is not True:
        raise NotReadOnlyError(
            f"{email} exists and is not a read-only demo account; refusing to delete it"
        )
    devices = cast(
        list[dict[str, Any]],
        client.table("devices")
        .select("device_id")
        .eq("user_id", user_id)
        .execute()
        .data,
    )
    for device in devices:
        for bucket in (SEGMENTS_BUCKET, REELS_BUCKET):
            _remove_prefix(client, bucket, str(device["device_id"]))
    client.auth.admin.delete_user(user_id)
    return True


def _remove_prefix(client: Any, bucket: str, prefix: str) -> None:
    storage = client.storage.from_(bucket)
    previous: list[str] = []
    while True:
        page = storage.list(prefix, {"limit": STORAGE_LIST_PAGE_SIZE, "offset": 0})
        names = [f"{prefix}/{obj['name']}" for obj in page]
        if not names:
            return
        if names == previous:
            raise RuntimeError(
                f"could not remove {bucket}/{prefix}/ objects: {names[:3]}"
            )
        storage.remove(names)
        previous = names


def create_demo_account(
    client: Any, email: str, password: str, device_name: str
) -> tuple[str, str]:
    """Create the read-only user and its device; returns (user_id, device_id)."""
    user_id = str(
        client.auth.admin.create_user(
            {"email": email, "password": password, "email_confirm": True}
        ).user.id
    )
    # Flagged in a second call: GoTrue's create finishes the user with an
    # update to its profile, which the read-only trigger would refuse.
    client.auth.admin.update_user_by_id(user_id, {"app_metadata": {"read_only": True}})
    rows = cast(
        list[dict[str, Any]],
        client.table("devices")
        .insert({"user_id": user_id, "name": device_name})
        .execute()
        .data,
    )
    return user_id, str(rows[0]["device_id"])


def upload_day(
    client: Any, device_id: str, segment_paths: list[Path], markers: list[str]
) -> None:
    bucket = client.storage.from_(SEGMENTS_BUCKET)
    for path in segment_paths:
        bucket.upload(
            f"{device_id}/{path.name}", path.read_bytes(), {"content-type": "video/mp4"}
        )
    for marker in markers:
        bucket.upload(
            f"{device_id}/{marker}", b"", {"content-type": "application/octet-stream"}
        )


def seed_demo(
    client: Any,
    *,
    email: str,
    password: str,
    day: date,
    plan: DayPlan,
    segment_paths: list[Path],
    transcriber: TranscriptionClient,
    vision: VisionClient,
    workdir: Path,
    device_name: str = DEFAULT_DEVICE_NAME,
) -> SeedResult:
    """Recreate the demo account, upload the rendered day and build its reel."""
    if remove_demo_account(client, email):
        logger.info("removed the previous demo account %s", email)
    user_id, device_id = create_demo_account(client, email, password, device_name)
    logger.info("created demo account %s with device %s", email, device_id)

    client.table("device_status").insert(
        {
            "device_id": device_id,
            **DEMO_DEVICE_STATUS,
            "segments_uploaded_today": len(segment_paths),
        }
    ).execute()
    upload_day(client, device_id, segment_paths, plan.markers)
    logger.info(
        "uploaded %d segments and %d flag markers",
        len(segment_paths),
        len(plan.markers),
    )

    push = RecordingPush()
    result = run_nightly(
        OrchestratorDeps(
            store=OneDeviceStore(client, device_id),
            media=FfmpegMediaProbe(),
            transcriber=transcriber,
            vision=vision,
            push=push,
            runner=SubprocessRunner(),
            workdir=workdir,
        ),
        day,
    )
    if result.devices_failed or result.devices_processed != 1:
        raise RuntimeError(f"the nightly pipeline failed for demo device {device_id}")

    reels = cast(
        list[dict[str, Any]],
        client.table("reels")
        .select("s3_key, duration_sec")
        .eq("device_id", device_id)
        .eq("date", day.isoformat())
        .execute()
        .data,
    )
    if not reels:
        raise RuntimeError(
            "the nightly pipeline ran but wrote no reel for the demo day"
        )
    flagged = cast(
        list[dict[str, Any]],
        client.table("segments")
        .select("id")
        .eq("device_id", device_id)
        .eq("manually_flagged", True)
        .execute()
        .data,
    )
    return SeedResult(
        user_id=user_id,
        device_id=device_id,
        day=day,
        segments=len(segment_paths),
        flagged_segments=len(flagged),
        reel_key=str(reels[0]["s3_key"]),
        reel_duration_sec=int(reels[0]["duration_sec"]),
    )
