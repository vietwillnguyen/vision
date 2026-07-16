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

    def upsert(self, rows):
        rows = rows if isinstance(rows, list) else [rows]
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
    def __init__(self, rows=None):
        self.rows = rows or []
        self.inserted = []
        self.updated = []
        self.upserted = []
        self.deleted = 0


class FakeBucket:
    def __init__(self, objects=None):
        self.objects = objects or []
        self.uploads = []

    def list(self, prefix):
        return [{"name": name} for name in self.objects]

    def download(self, key):
        return b"video-bytes"

    def upload(self, key, data):
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
        "pipeline_dlq": FakeTable(),
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

        assert tables["pipeline_dlq"].upserted == [
            {
                "device_id": "dev-1",
                "key": "dev-1/x.mp4",
                "kind": "rejected",
                "attempts": 1,
                "escalated": False,
            }
        ]

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

    def test_insert_reel_inserts_row(self):
        store, tables = make_store()
        row = {"device_id": "dev-1", "date": "2026-07-14", "s3_key": "k"}

        store.insert_reel(row)

        assert tables["reels"].inserted == [row]
