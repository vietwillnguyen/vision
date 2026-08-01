"""Supabase-backed PipelineStore adapter.

Runs with the service_role key (the pipeline is trusted backend code; RLS
still protects every other client). Segment rows have no unique constraint
on ``s3_key``, so persistence selects existing keys and splits into
insert-new versus update-scores rather than relying on upsert.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from pipeline.models import ScoreWeights
from pipeline.orchestrator import DeviceRecord, DlqEntry

SEGMENTS_BUCKET = "segments"
REELS_BUCKET = "reels"
DLQ_TABLE = "pipeline_dlq"
STORAGE_LIST_PAGE_SIZE = 100


class SupabaseStore:
    # `client` is supabase-py's Client in production and a fake in tests.
    # Annotating it as Client would reject the fakes this suite is built on, and
    # supabase-py ships no Protocol for its fluent query builder - so the
    # untyped boundary is stated explicitly as Any rather than left implicit.
    def __init__(self, client: Any) -> None:
        self._client = client

    def list_devices(self) -> list[DeviceRecord]:
        rows = (
            self._client.table("devices")
            .select("device_id, user_id, push_token")
            .execute()
            .data
        )
        return [
            DeviceRecord(
                device_id=row["device_id"],
                user_id=row["user_id"],
                push_token=row.get("push_token"),
            )
            for row in rows
        ]

    def list_segment_object_keys(self, device_id: str, day: date) -> list[str]:
        prefix = day.strftime("%Y%m%d")
        return [
            f"{device_id}/{name}"
            for name in self._list_bucket_names(device_id)
            if name.startswith(f"{prefix}_") and name.endswith(".mp4")
        ]

    def list_flag_marker_keys(self, device_id: str, day: date) -> list[str]:
        prefix = day.strftime("%Y%m%d")
        return [
            f"{device_id}/{name}"
            for name in self._list_bucket_names(device_id)
            if name.startswith(f"FLAG_{prefix}_") and name.endswith(".marker")
        ]

    def _list_bucket_names(self, device_id: str) -> list[str]:
        names: list[str] = []
        offset = 0
        while True:
            page = self._client.storage.from_(SEGMENTS_BUCKET).list(
                device_id,
                {
                    "limit": STORAGE_LIST_PAGE_SIZE,
                    "offset": offset,
                    "sortBy": {"column": "name", "order": "asc"},
                },
            )
            names.extend(obj["name"] for obj in page)
            if len(page) < STORAGE_LIST_PAGE_SIZE:
                return names
            offset += STORAGE_LIST_PAGE_SIZE

    def fetch_score_weights(self, user_id: str) -> ScoreWeights:
        rows = (
            self._client.table("score_weights")
            .select("scene_weight, audio_weight, motion_weight")
            .eq("user_id", user_id)
            .execute()
            .data
        )
        if not rows:
            return ScoreWeights()
        row = rows[0]
        return ScoreWeights(
            scene_weight=float(row["scene_weight"]),
            audio_weight=float(row["audio_weight"]),
            motion_weight=float(row["motion_weight"]),
        )

    def persist_segments(self, rows: list[dict]) -> None:
        if not rows:
            return
        keys = [row["s3_key"] for row in rows]
        existing = (
            self._client.table("segments")
            .select("s3_key")
            .in_("s3_key", keys)
            .execute()
            .data
        )
        existing_keys = {row["s3_key"] for row in existing}
        new_rows = [row for row in rows if row["s3_key"] not in existing_keys]
        if new_rows:
            self._client.table("segments").insert(new_rows).execute()
        for row in rows:
            if row["s3_key"] in existing_keys:
                self._client.table("segments").update(
                    {
                        "composite_score": row["composite_score"],
                        "manually_flagged": row["manually_flagged"],
                    }
                ).eq("s3_key", row["s3_key"]).execute()

    def download_segment(self, s3_key: str, workdir: Path) -> Path:
        data = self._client.storage.from_(SEGMENTS_BUCKET).download(s3_key)
        local = workdir / Path(s3_key).name
        local.write_bytes(data)
        return local

    def upload_reel(self, device_id: str, day: date, local_path: Path) -> str:
        key = f"{device_id}/{day.strftime('%Y%m%d')}.mp4"
        self._client.storage.from_(REELS_BUCKET).upload(
            key, local_path.read_bytes(), file_options={"upsert": "true"}
        )
        return key

    def insert_reel(self, row: dict) -> None:
        self._client.table("reels").upsert(row, on_conflict="device_id,date").execute()

    def list_segment_rows(self, device_id: str, day: date) -> list[dict]:
        day_start = datetime.combine(day, datetime.min.time())
        return (
            self._client.table("segments")
            .select("s3_key, recorded_at, duration_sec")
            .eq("device_id", device_id)
            .gte("recorded_at", day_start.isoformat())
            .lt("recorded_at", (day_start + timedelta(days=1)).isoformat())
            .execute()
            .data
        )

    def flag_segment(self, s3_key: str) -> None:
        (
            self._client.table("segments")
            .update({"manually_flagged": True})
            .eq("s3_key", s3_key)
            .execute()
        )

    def clear_push_token(self, device_id: str) -> None:
        (
            self._client.table("devices")
            .update({"push_token": None})
            .eq("device_id", device_id)
            .execute()
        )

    def load_pending_dlq(self, device_id: str) -> list[DlqEntry]:
        rows = (
            self._client.table(DLQ_TABLE)
            .select("key, kind, attempts")
            .eq("device_id", device_id)
            .eq("escalated", False)
            .execute()
            .data
        )
        return [
            DlqEntry(key=row["key"], kind=row["kind"], attempts=row["attempts"])
            for row in rows
        ]

    def record_dlq(self, device_id: str, entries: list[DlqEntry]) -> None:
        escalated_rows = (
            self._client.table(DLQ_TABLE)
            .select("key")
            .eq("device_id", device_id)
            .eq("escalated", True)
            .in_("key", [entry.key for entry in entries])
            .execute()
            .data
        )
        escalated_keys = {row["key"] for row in escalated_rows}
        entries = [entry for entry in entries if entry.key not in escalated_keys]
        if not entries:
            return
        now = datetime.now(UTC).isoformat()
        self._client.table(DLQ_TABLE).upsert(
            [
                {
                    "device_id": device_id,
                    "key": entry.key,
                    "kind": entry.kind,
                    "attempts": entry.attempts,
                    "escalated": False,
                    "updated_at": now,
                }
                for entry in entries
            ]
        ).execute()

    def resolve_dlq(self, device_id: str, keys: list[str]) -> None:
        (
            self._client.table(DLQ_TABLE)
            .delete()
            .eq("device_id", device_id)
            .in_("key", keys)
            .execute()
        )

    def escalate_dlq(self, device_id: str, keys: list[str]) -> None:
        (
            self._client.table(DLQ_TABLE)
            .update({"escalated": True})
            .eq("device_id", device_id)
            .in_("key", keys)
            .execute()
        )
