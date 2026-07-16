from datetime import date
from pathlib import Path

from pipeline.adapters.supabase_store import SupabaseStore
from pipeline.models import ScoreWeights
from pipeline.orchestrator import DlqEntry

DAY = date(2026, 7, 14)


class FakeExecuteResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, table):
        self._table = table
        self._filters = []
        self._pending_update = None
        self._pending_delete = False

    def select(self, *_columns):
        return self

    def eq(self, column, value):
        self._filters.append(lambda row: row.get(column) == value)
        return self

    def in_(self, column, values):
        self._filters.append(lambda row: row.get(column) in values)
        return self

    def insert(self, rows):
        rows = rows if isinstance(rows, list) else [rows]
        self._table.inserted.extend(rows)
        self._table.rows.extend(rows)
        return self

    def update(self, values):
        self._pending_update = values
        return self

    def gte(self, column, value):
        self._filters.append(lambda row: row.get(column) >= value)
        return self

    def lt(self, column, value):
        self._filters.append(lambda row: row.get(column) < value)
        return self

    def upsert(self, rows, on_conflict=None):
        rows = rows if isinstance(rows, list) else [rows]
        conflict_columns = on_conflict.split(",") if on_conflict else self._table.pk
        for row in rows:
            existing = None
            if conflict_columns:
                for candidate in self._table.rows:
                    if all(candidate.get(c) == row.get(c) for c in conflict_columns):
                        existing = candidate
                        break
            if existing is not None:
                existing.update(row)
            else:
                self._table.rows.append(row)
        self._table.upserted.extend(rows)
        return self

    def delete(self):
        self._pending_delete = True
        return self

    def _matching_rows(self):
        return [r for r in self._table.rows if all(f(r) for f in self._filters)]

    def execute(self):
        if self._pending_update is not None:
            matched = self._matching_rows()
            for row in matched:
                row.update(self._pending_update)
            self._table.updated.append(self._pending_update)
            return FakeExecuteResult(matched)
        if self._pending_delete:
            self._table.rows = [
                r for r in self._table.rows if not all(f(r) for f in self._filters)
            ]
            self._table.deleted += 1
            return FakeExecuteResult([])
        return FakeExecuteResult(self._matching_rows())


class FakeTable:
    def __init__(self, rows=None, pk=None):
        self.rows = rows or []
        self.pk = pk
        self.inserted = []
        self.updated = []
        self.upserted = []
        self.deleted = 0


class FakeBucket:
    """Models supabase storage: list truncates to limit (default 100) and
    upload raises a 409-style error on duplicate keys unless upsert is set."""

    DEFAULT_LIST_LIMIT = 100

    def __init__(self, objects=None):
        self.objects = objects or []
        self.uploads = []
        self.list_calls = []

    def list(self, prefix, options=None):
        options = options or {}
        self.list_calls.append(options)
        limit = options.get("limit", self.DEFAULT_LIST_LIMIT)
        offset = options.get("offset", 0)
        names = sorted(self.objects)
        return [{"name": name} for name in names[offset : offset + limit]]

    def download(self, key):
        return b"video-bytes"

    def upload(self, key, data, file_options=None):
        upsert = (file_options or {}).get("upsert") == "true"
        if any(k == key for k, _ in self.uploads) and not upsert:
            raise RuntimeError("409 Duplicate: The resource already exists")
        self.uploads.append((key, data))


class FakeStorage:
    def __init__(self, buckets):
        self._buckets = buckets

    def from_(self, bucket):
        return self._buckets[bucket]


class FakeSupabaseClient:
    def __init__(self, tables, buckets=None):
        self._tables = tables
        self.storage = FakeStorage(buckets or {})

    def table(self, name):
        return FakeQuery(self._tables[name])


def make_store(tables=None, buckets=None):
    defaults = {
        "devices": FakeTable(),
        "segments": FakeTable(),
        "reels": FakeTable(),
        "score_weights": FakeTable(),
        "pipeline_dlq": FakeTable(pk=["device_id", "key"]),
    }
    defaults.update(tables or {})
    return SupabaseStore(FakeSupabaseClient(defaults, buckets)), defaults


class TestStorageListing:
    def test_segment_keys_filtered_to_day_and_mp4(self):
        bucket = FakeBucket(
            objects=[
                "20260714_090000.mp4",
                "20260714_091500.mp4",
                "20260713_090000.mp4",
                "FLAG_20260714_090100.marker",
            ]
        )
        store, _ = make_store(buckets={"segments": bucket, "reels": FakeBucket()})

        keys = store.list_segment_object_keys("dev-1", DAY)

        assert keys == ["dev-1/20260714_090000.mp4", "dev-1/20260714_091500.mp4"]

    def test_marker_keys_filtered_to_day(self):
        bucket = FakeBucket(
            objects=[
                "FLAG_20260714_090100.marker",
                "FLAG_20260713_090100.marker",
                "20260714_090000.mp4",
            ]
        )
        store, _ = make_store(buckets={"segments": bucket, "reels": FakeBucket()})

        keys = store.list_flag_marker_keys("dev-1", DAY)

        assert keys == ["dev-1/FLAG_20260714_090100.marker"]

    def test_listing_paginates_past_storage_page_limit(self):
        objects = [f"20260714_{i // 60:02d}{i % 60:02d}00.mp4" for i in range(250)]
        bucket = FakeBucket(objects=objects)
        store, _ = make_store(buckets={"segments": bucket, "reels": FakeBucket()})

        keys = store.list_segment_object_keys("dev-1", DAY)

        assert len(keys) == 250
        assert len(bucket.list_calls) == 3
        assert all("limit" in call and "offset" in call for call in bucket.list_calls)


class TestPersistSegments:
    def test_inserts_new_rows_and_updates_existing_by_s3_key(self):
        existing = {
            "s3_key": "dev-1/20260714_090000.mp4",
            "composite_score": 0.0,
            "manually_flagged": False,
        }
        store, tables = make_store(tables={"segments": FakeTable(rows=[existing])})

        store.persist_segments(
            [
                {
                    "device_id": "dev-1",
                    "s3_key": "dev-1/20260714_090000.mp4",
                    "recorded_at": "2026-07-14T09:00:00",
                    "duration_sec": 300,
                    "composite_score": 0.7,
                    "manually_flagged": True,
                },
                {
                    "device_id": "dev-1",
                    "s3_key": "dev-1/20260714_091500.mp4",
                    "recorded_at": "2026-07-14T09:15:00",
                    "duration_sec": 300,
                    "composite_score": 0.4,
                    "manually_flagged": False,
                },
            ]
        )

        segments = tables["segments"]
        assert len(segments.inserted) == 1
        assert segments.inserted[0]["s3_key"] == "dev-1/20260714_091500.mp4"
        assert existing["composite_score"] == 0.7
        assert existing["manually_flagged"] is True


class TestScoreWeights:
    def test_returns_defaults_when_user_has_no_row(self):
        store, _ = make_store()

        assert store.fetch_score_weights("user-1") == ScoreWeights()

    def test_returns_user_row_values(self):
        row = {
            "user_id": "user-1",
            "scene_weight": 0.5,
            "audio_weight": 0.25,
            "motion_weight": 0.25,
        }
        store, _ = make_store(tables={"score_weights": FakeTable(rows=[row])})

        weights = store.fetch_score_weights("user-1")

        assert weights == ScoreWeights(
            scene_weight=0.5, audio_weight=0.25, motion_weight=0.25
        )


class TestDevices:
    def test_maps_rows_to_device_records(self):
        rows = [
            {"device_id": "dev-1", "user_id": "user-1", "push_token": "tok"},
            {"device_id": "dev-2", "user_id": "user-2", "push_token": None},
        ]
        store, _ = make_store(tables={"devices": FakeTable(rows=rows)})

        devices = store.list_devices()

        assert [d.device_id for d in devices] == ["dev-1", "dev-2"]
        assert devices[0].push_token == "tok"
        assert devices[1].push_token is None


class TestDlq:
    def test_load_pending_skips_escalated_rows(self):
        rows = [
            {
                "device_id": "dev-1",
                "key": "dev-1/a.marker",
                "kind": "unmatched",
                "attempts": 2,
                "escalated": False,
            },
            {
                "device_id": "dev-1",
                "key": "dev-1/b.marker",
                "kind": "unmatched",
                "attempts": 5,
                "escalated": True,
            },
        ]
        store, _ = make_store(tables={"pipeline_dlq": FakeTable(rows=rows)})

        pending = store.load_pending_dlq("dev-1")

        assert pending == [DlqEntry(key="dev-1/a.marker", kind="unmatched", attempts=2)]

    def test_record_upserts_entries(self):
        store, tables = make_store()

        store.record_dlq(
            "dev-1", [DlqEntry(key="dev-1/x.mp4", kind="rejected", attempts=1)]
        )

        assert len(tables["pipeline_dlq"].upserted) == 1
        upserted = tables["pipeline_dlq"].upserted[0]
        assert upserted["device_id"] == "dev-1"
        assert upserted["key"] == "dev-1/x.mp4"
        assert upserted["kind"] == "rejected"
        assert upserted["attempts"] == 1
        assert upserted["escalated"] is False

    def test_record_refreshes_updated_at_on_retry(self):
        store, tables = make_store()

        store.record_dlq(
            "dev-1", [DlqEntry(key="dev-1/x.mp4", kind="rejected", attempts=1)]
        )
        first = tables["pipeline_dlq"].rows[0]["updated_at"]
        store.record_dlq(
            "dev-1", [DlqEntry(key="dev-1/x.mp4", kind="rejected", attempts=2)]
        )

        assert len(tables["pipeline_dlq"].rows) == 1
        row = tables["pipeline_dlq"].rows[0]
        assert row["attempts"] == 2
        assert row["updated_at"] >= first

    def test_record_does_not_resurrect_escalated_row(self):
        escalated_row = {
            "device_id": "dev-1",
            "key": "dev-1/x.mp4",
            "kind": "rejected",
            "attempts": 5,
            "escalated": True,
        }
        store, tables = make_store(
            tables={
                "pipeline_dlq": FakeTable(
                    rows=[escalated_row], pk=["device_id", "key"]
                )
            }
        )

        store.record_dlq(
            "dev-1", [DlqEntry(key="dev-1/x.mp4", kind="rejected", attempts=1)]
        )

        assert tables["pipeline_dlq"].rows == [escalated_row]
        assert escalated_row["escalated"] is True
        assert escalated_row["attempts"] == 5

    def test_record_still_upserts_non_escalated_entries(self):
        escalated_row = {
            "device_id": "dev-1",
            "key": "dev-1/x.mp4",
            "kind": "rejected",
            "attempts": 5,
            "escalated": True,
        }
        store, tables = make_store(
            tables={
                "pipeline_dlq": FakeTable(
                    rows=[escalated_row], pk=["device_id", "key"]
                )
            }
        )

        store.record_dlq(
            "dev-1",
            [
                DlqEntry(key="dev-1/x.mp4", kind="rejected", attempts=1),
                DlqEntry(key="dev-1/y.mp4", kind="rejected", attempts=1),
            ],
        )

        keys = {r["key"]: r for r in tables["pipeline_dlq"].rows}
        assert keys["dev-1/x.mp4"]["escalated"] is True
        assert keys["dev-1/x.mp4"]["attempts"] == 5
        assert keys["dev-1/y.mp4"]["escalated"] is False
        assert keys["dev-1/y.mp4"]["attempts"] == 1

    def test_resolve_deletes_keys(self):
        rows = [
            {"device_id": "dev-1", "key": "dev-1/a.marker"},
            {"device_id": "dev-1", "key": "dev-1/b.marker"},
        ]
        store, tables = make_store(tables={"pipeline_dlq": FakeTable(rows=rows)})

        store.resolve_dlq("dev-1", ["dev-1/a.marker"])

        assert [r["key"] for r in tables["pipeline_dlq"].rows] == ["dev-1/b.marker"]

    def test_escalate_marks_rows_escalated(self):
        rows = [
            {
                "device_id": "dev-1",
                "key": "dev-1/a.marker",
                "attempts": 4,
                "escalated": False,
            }
        ]
        store, tables = make_store(tables={"pipeline_dlq": FakeTable(rows=rows)})

        store.escalate_dlq("dev-1", ["dev-1/a.marker"])

        assert rows[0]["escalated"] is True


class TestClearPushToken:
    def test_clear_push_token_nulls_the_device_token(self):
        row = {"device_id": "dev-1", "user_id": "user-1", "push_token": "tok"}
        store, _ = make_store(tables={"devices": FakeTable(rows=[row])})

        store.clear_push_token("dev-1")

        assert row["push_token"] is None


class TestReelsAndBlobs:
    def test_download_segment_writes_bytes_to_workdir(self, tmp_path):
        store, _ = make_store(
            buckets={"segments": FakeBucket(), "reels": FakeBucket()}
        )

        local = store.download_segment("dev-1/20260714_090000.mp4", tmp_path)

        assert local == tmp_path / "20260714_090000.mp4"
        assert local.read_bytes() == b"video-bytes"

    def test_upload_reel_targets_device_day_key(self, tmp_path):
        reels = FakeBucket()
        store, _ = make_store(buckets={"segments": FakeBucket(), "reels": reels})
        reel_file = tmp_path / "reel.mp4"
        reel_file.write_bytes(b"reel")

        key = store.upload_reel("dev-1", DAY, reel_file)

        assert key == "dev-1/20260714.mp4"
        assert reels.uploads == [("dev-1/20260714.mp4", b"reel")]

    def test_upload_reel_same_day_rerun_overwrites_instead_of_409(self, tmp_path):
        reels = FakeBucket()
        store, _ = make_store(buckets={"segments": FakeBucket(), "reels": reels})
        reel_file = tmp_path / "reel.mp4"
        reel_file.write_bytes(b"reel-v1")

        store.upload_reel("dev-1", DAY, reel_file)
        reel_file.write_bytes(b"reel-v2")
        store.upload_reel("dev-1", DAY, reel_file)

        assert reels.uploads[-1] == ("dev-1/20260714.mp4", b"reel-v2")

    def test_insert_reel_upserts_row(self):
        store, tables = make_store()
        row = {"device_id": "dev-1", "date": "2026-07-14", "s3_key": "k"}

        store.insert_reel(row)

        assert tables["reels"].rows == [row]

    def test_insert_reel_same_device_day_rerun_keeps_single_row(self):
        store, tables = make_store()

        store.insert_reel(
            {"device_id": "dev-1", "date": "2026-07-14", "s3_key": "k", "duration_sec": 90}
        )
        store.insert_reel(
            {"device_id": "dev-1", "date": "2026-07-14", "s3_key": "k", "duration_sec": 60}
        )

        assert len(tables["reels"].rows) == 1
        assert tables["reels"].rows[0]["duration_sec"] == 60


class TestSegmentRows:
    def test_list_segment_rows_filters_to_device_and_day(self):
        rows = [
            {
                "device_id": "dev-1",
                "s3_key": "dev-1/20260714_090000.mp4",
                "recorded_at": "2026-07-14T09:00:00",
                "duration_sec": 300,
            },
            {
                "device_id": "dev-1",
                "s3_key": "dev-1/20260713_090000.mp4",
                "recorded_at": "2026-07-13T09:00:00",
                "duration_sec": 300,
            },
            {
                "device_id": "dev-2",
                "s3_key": "dev-2/20260714_090000.mp4",
                "recorded_at": "2026-07-14T09:00:00",
                "duration_sec": 300,
            },
        ]
        store, _ = make_store(tables={"segments": FakeTable(rows=rows)})

        result = store.list_segment_rows("dev-1", DAY)

        assert [r["s3_key"] for r in result] == ["dev-1/20260714_090000.mp4"]

    def test_flag_segment_sets_manually_flagged(self):
        row = {
            "device_id": "dev-1",
            "s3_key": "dev-1/20260714_090000.mp4",
            "manually_flagged": False,
        }
        store, _ = make_store(tables={"segments": FakeTable(rows=[row])})

        store.flag_segment("dev-1/20260714_090000.mp4")

        assert row["manually_flagged"] is True
