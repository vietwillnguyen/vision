"""Pipeline row-shape contract test.

Guarantee: *the row dicts ``pipeline.orchestrator.run_nightly`` writes match the
columns those tables actually have.*

The app half of this contract - that the reel and segment rows the app reads
have the fields its hooks expect - is no longer checked here. It is enforced by
the compiler instead: ``app/src/lib/supabase.ts`` builds its client as
``createClient<Database>`` over the generated ``app/src/generated/database.ts``, so
every ``.from(...).select(...)`` in the app resolves to the real column list and
``tsc --noEmit`` (a gate in both lint.yml and tests.yml) rejects a field the
schema does not have. That covers the whole app rather than the one hook a
regex was taught to read.

Generated TypeScript does nothing for ``pipeline/``, so this half stays in
Python - but sourced from the schema rather than from a regex over DDL.
``tests/fixtures/public_schema.json`` is ``information_schema.columns`` read out
of a live local Supabase instance by ``scripts/gen-db-types.sh``, the same
script and the same instance that produce the TypeScript types. CI regenerates
both and fails on any diff, so a migration cannot land without updating them.

Rather than mocking pipeline's own persistence boundary, this drives the *real*
``run_nightly`` with only the external-cost boundaries faked (media probe,
transcriber, vision, ffmpeg command runner, push), then checks the real row
dicts it produces against that fixture. It needs no live instance itself.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from pipeline.orchestrator import DeviceRecord, OrchestratorDeps, run_nightly

SCHEMA_FIXTURE = Path(__file__).parent / "fixtures" / "public_schema.json"


def _load_columns(table_name: str) -> dict[str, dict]:
    """Return {column_name: column_metadata} for a public base table.

    Asserts the fixture actually describes the table: the regex-parsing
    predecessor of this test passed green whenever its own parsing found
    nothing, and an empty column set would make every check below vacuous.
    """
    schema = json.loads(SCHEMA_FIXTURE.read_text())
    tables = schema["tables"]
    assert table_name in tables, (
        f"{SCHEMA_FIXTURE.name} has no {table_name} table; "
        "regenerate it with scripts/gen-db-types.sh"
    )
    columns = tables[table_name]
    assert columns, f"{table_name} has no columns in {SCHEMA_FIXTURE.name}"
    return columns


def _required_columns(columns: dict[str, dict]) -> set[str]:
    """Columns a writer must supply: not nullable and with no default."""
    return {
        name
        for name, meta in columns.items()
        if not meta["is_nullable"] and not meta["has_default"]
    }


class FakeStore:
    def __init__(self, devices, segment_keys):
        self.devices = devices
        self.segment_keys = segment_keys
        self.persisted_segments: list[dict] = []
        self.inserted_reels: list[dict] = []

    def list_devices(self):
        return self.devices

    def list_segment_object_keys(self, device_id, day):
        return self.segment_keys.get(device_id, [])

    def list_flag_marker_keys(self, device_id, day):
        return []

    def load_pending_dlq(self, device_id):
        return []

    def fetch_score_weights(self, user_id):
        from pipeline.models import ScoreWeights

        return ScoreWeights()

    def persist_segments(self, rows):
        self.persisted_segments.extend(rows)

    def download_segment(self, s3_key, workdir):
        local = workdir / Path(s3_key).name
        local.write_bytes(b"video")
        return local

    def upload_reel(self, device_id, day, local_path):
        return f"{device_id}/{day.strftime('%Y%m%d')}.mp4"

    def insert_reel(self, row):
        self.inserted_reels.append(row)

    def record_dlq(self, device_id, entries):
        pass

    def resolve_dlq(self, device_id, keys):
        pass

    def escalate_dlq(self, device_id, keys):
        pass

    def list_segment_rows(self, device_id, day):
        return []

    def flag_segment(self, s3_key):
        pass

    def clear_push_token(self, device_id):
        pass


class FakeMedia:
    def extract_audio(self, video_path, workdir):
        audio = workdir / (video_path.stem + ".wav")
        audio.write_bytes(b"audio")
        return audio

    def extract_frame_diffs(self, video_path):
        return [200.0]

    def extract_frames(self, video_path, workdir):
        frame = workdir / (video_path.stem + ".jpg")
        frame.write_bytes(b"jpg")
        return [frame]


class FakeTranscriber:
    def transcribe(self, audio_path):
        from pipeline.scoring.audio import TranscriptionResult

        return TranscriptionResult(
            speech_presence_ratio=0.8, silence_ratio=0.1, has_exclamation=False
        )


class FakeVision:
    def score_frames(self, frame_paths, prompt):
        return '{"score": 8, "location": "indoor", "people": true}'


class FakePush:
    def send(self, to_token, title, body):
        pass


class FakeRunner:
    def run(self, command):
        Path(command[-1]).write_bytes(b"out")


@pytest.fixture
def nightly_run(tmp_path):
    """Run the real orchestrator once and hand back what it tried to write."""
    device = DeviceRecord(device_id="dev-1", user_id="user-1", push_token=None)
    store = FakeStore(
        devices=[device],
        segment_keys={"dev-1": ["dev-1/20260714_090000.mp4"]},
    )
    deps = OrchestratorDeps(
        store=store,
        media=FakeMedia(),
        transcriber=FakeTranscriber(),
        vision=FakeVision(),
        push=FakePush(),
        runner=FakeRunner(),
        workdir=tmp_path,
    )

    run_nightly(deps, date(2026, 7, 14))
    return store


def test_segment_row_matches_migration_columns(nightly_run):
    assert len(nightly_run.persisted_segments) == 1
    row = nightly_run.persisted_segments[0]

    columns = _load_columns("segments")
    unknown = set(row) - set(columns)
    assert not unknown, f"pipeline writes unknown segment columns: {unknown}"
    missing = _required_columns(columns) - set(row)
    assert not missing, f"pipeline's segment row is missing required columns: {missing}"


def test_reel_row_matches_migration_columns(nightly_run):
    assert len(nightly_run.inserted_reels) == 1
    row = nightly_run.inserted_reels[0]

    columns = _load_columns("reels")
    unknown = set(row) - set(columns)
    assert not unknown, f"pipeline writes unknown reel columns: {unknown}"
    missing = _required_columns(columns) - set(row)
    assert not missing, f"pipeline's reel row is missing required columns: {missing}"
