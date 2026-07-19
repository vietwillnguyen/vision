"""Cross-service row-shape contract tests.

Stage 0 of the test-validation-plan integration suite (see
.lavish/test-validation-plan.html): asserts two of the "Now covered locally"
bullets without needing a live Supabase/Postgres instance (Docker is
unavailable in this sandbox, so the full supabase-start chain from the plan's
Stages 1-3 is still deferred):

  * "Segment row shape written by firmware matches what pipeline queries"
  * "Reel row + storage object shape written by pipeline matches what app's
    hooks expect"

Rather than mocking pipeline's own persistence boundary, this drives the
*real* ``pipeline.orchestrator.run_nightly`` with only the plan's named
external-cost boundaries faked (media probe, transcriber, vision, ffmpeg
command runner, push) - exactly the "LLM/FFmpeg calls stubbed" carve-out the
plan describes for Stage 2 - then checks the real row dicts it produces
against two independent, real sources of truth:

  * the actual `create table` column list in the supabase migrations, parsed
    from the SQL text (not hand-copied), so a schema change breaks this test
    instead of silently drifting
  * the actual field accesses in app/src/hooks/useReel.ts's mapReelRow(),
    parsed from the TypeScript source, so an app-side rename also breaks
    this test
"""

import re
from datetime import date
from pathlib import Path

from pipeline.orchestrator import DeviceRecord, OrchestratorDeps, run_nightly

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "supabase" / "migrations"
USE_REEL_TS = REPO_ROOT / "app" / "src" / "hooks" / "useReel.ts"


def _parse_create_table_columns(sql_text: str, table_name: str) -> dict[str, dict]:
    """Parse a `create table public.<table_name> (...)` block into
    {column_name: {"not_null": bool, "has_default": bool}}, skipping
    table-level constraint lines (primary key/foreign key/unique/check).
    """
    match = re.search(
        rf"create table public\.{re.escape(table_name)}\s*\((.*)\)\s*;",
        sql_text,
        re.S,
    )
    assert match, f"no create table for {table_name} found"
    body = match.group(1)

    parts: list[str] = []
    current = ""
    depth = 0
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)

    columns: dict[str, dict] = {}
    skip_keywords = {"primary", "foreign", "unique", "check", "constraint"}
    for part in parts:
        part = part.strip()
        if not part:
            continue
        name = part.split()[0]
        if name.lower() in skip_keywords:
            continue
        lowered = part.lower()
        columns[name] = {
            "not_null": "not null" in lowered,
            "has_default": "default" in lowered,
        }
    return columns


def _table_columns(migration_glob: str, table_name: str) -> dict[str, dict]:
    matches = list(MIGRATIONS_DIR.glob(migration_glob))
    assert matches, f"no migration matching {migration_glob} in {MIGRATIONS_DIR}"
    sql_text = matches[0].read_text()
    return _parse_create_table_columns(sql_text, table_name)


def _required_columns_without_default(columns: dict[str, dict]) -> set[str]:
    return {
        name
        for name, meta in columns.items()
        if meta["not_null"] and not meta["has_default"]
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


def _run_pipeline_once():
    device = DeviceRecord(device_id="dev-1", user_id="user-1", push_token=None)
    day = date(2026, 7, 14)
    store = FakeStore(
        devices=[device],
        segment_keys={"dev-1": ["dev-1/20260714_090000.mp4"]},
    )
    return store, device, day


def test_segment_row_matches_required_migration_columns(tmp_path):
    store, device, day = _run_pipeline_once()
    deps = OrchestratorDeps(
        store=store,
        media=FakeMedia(),
        transcriber=FakeTranscriber(),
        vision=FakeVision(),
        push=FakePush(),
        runner=FakeRunner(),
        workdir=tmp_path,
    )

    run_nightly(deps, day)

    assert len(store.persisted_segments) == 1
    row = store.persisted_segments[0]

    columns = _table_columns("*_create_segments.sql", "segments")
    assert set(row.keys()) <= set(columns.keys()), (
        f"pipeline writes unknown segment columns: {set(row.keys()) - set(columns.keys())}"
    )
    required = _required_columns_without_default(columns) - {"id"}
    assert required <= set(row.keys()), (
        f"pipeline's segment row is missing required columns: {required - set(row.keys())}"
    )


def test_reel_row_matches_required_migration_columns_and_app_hook_fields(tmp_path):
    store, device, day = _run_pipeline_once()
    deps = OrchestratorDeps(
        store=store,
        media=FakeMedia(),
        transcriber=FakeTranscriber(),
        vision=FakeVision(),
        push=FakePush(),
        runner=FakeRunner(),
        workdir=tmp_path,
    )

    run_nightly(deps, day)

    assert len(store.inserted_reels) == 1
    row = store.inserted_reels[0]

    columns = _table_columns("*_create_reels.sql", "reels")
    assert set(row.keys()) <= set(columns.keys()), (
        f"pipeline writes unknown reel columns: {set(row.keys()) - set(columns.keys())}"
    )
    required = _required_columns_without_default(columns) - {"id", "created_at"}
    assert required <= set(row.keys()), (
        f"pipeline's reel row is missing required columns: {required - set(row.keys())}"
    )

    ts_source = USE_REEL_TS.read_text()
    app_fields = set(re.findall(r"row\.(\w+)\s+as\s", ts_source))
    assert app_fields, "expected to find `row.<field> as <Type>` accesses in useReel.ts"
    # id/created_at are DB-generated (not in pipeline's insert payload) but are
    # present on every real row Supabase returns, so the app can read them.
    db_generated = {"id", "created_at"}
    available_to_app = set(row.keys()) | db_generated
    assert app_fields <= available_to_app, (
        f"useReel.ts reads fields the reel row never has: {app_fields - available_to_app}"
    )
