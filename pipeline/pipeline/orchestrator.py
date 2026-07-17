"""Nightly per-device orchestration of the reel pipeline.

Wires ingestion -> flag markers -> cost-gated scoring -> selection ->
assembly -> delivery against injectable boundaries, so the run is fully
unit-testable with fakes; real adapters live in ``pipeline.adapters``.

Rejected keys and unmatched flag markers follow the DLQ policy from the
pipeline plan's Handoff section: persist with attempt counts, retry on every
nightly run, and once a key exhausts ``MAX_DLQ_ATTEMPTS`` escalate it and
notify the device owner instead of retrying forever. Marker filenames carry
their full date, so retried markers can never flag another day's segment;
unmatched markers are also matched against segments already persisted for the
marker's own date, so a marker retried after its day was processed flags its
segment and resolves instead of escalating. Recovered DLQ rows are only
resolved after the device's day completes, so a later failure cannot lose a
not-yet-persisted recovery.

Push delivery failures never fail an otherwise-successful device day: they
are logged, a permanently dead token (DeviceNotRegistered) clears the
device's stored push_token, and a DLQ row is only marked escalated once the
owner notification was delivered or is undeliverable, so a transient push
failure retries the escalation on the next run.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from pipeline.assembly.ffmpeg_assembler import (
    build_assembly_command,
    build_trim_command,
    compute_clip_offset_sec,
    render_concat_file_content,
)
from pipeline.delivery.notifier import (
    PushClient,
    PushTokenNotRegisteredError,
    notify_reel_ready,
)
from pipeline.ingestion import (
    apply_flag_markers,
    build_segments_from_object_keys,
    parse_flag_marker_filename,
)
from pipeline.models import ScoreWeights, Segment
from pipeline.scoring.audio import TranscriptionClient
from pipeline.scoring.scene import VisionClient
from pipeline.scoring_pipeline import score_segment
from pipeline.selection.highlight_selector import CLIP_DURATION_SEC, select_highlights

logger = logging.getLogger(__name__)

MAX_DLQ_ATTEMPTS = 5
ESCALATION_TITLE = "Some footage needs attention"
ESCALATION_BODY = "Some of your footage could not be processed after repeated attempts."


@dataclass
class DeviceRecord:
    device_id: str
    user_id: str
    push_token: str | None


@dataclass
class DlqEntry:
    key: str
    kind: str  # "rejected" | "unmatched"
    attempts: int


class PipelineStore(Protocol):
    def list_devices(self) -> list[DeviceRecord]: ...

    def list_segment_object_keys(self, device_id: str, day: date) -> list[str]: ...

    def list_flag_marker_keys(self, device_id: str, day: date) -> list[str]: ...

    def load_pending_dlq(self, device_id: str) -> list[DlqEntry]: ...

    def fetch_score_weights(self, user_id: str) -> ScoreWeights: ...

    def persist_segments(self, rows: list[dict]) -> None: ...

    def download_segment(self, s3_key: str, workdir: Path) -> Path: ...

    def upload_reel(self, device_id: str, day: date, local_path: Path) -> str: ...

    def insert_reel(self, row: dict) -> None: ...

    def record_dlq(self, device_id: str, entries: list[DlqEntry]) -> None: ...

    def resolve_dlq(self, device_id: str, keys: list[str]) -> None: ...

    def escalate_dlq(self, device_id: str, keys: list[str]) -> None: ...

    def list_segment_rows(self, device_id: str, day: date) -> list[dict]: ...

    def flag_segment(self, s3_key: str) -> None: ...

    def clear_push_token(self, device_id: str) -> None: ...


class MediaProbe(Protocol):
    def extract_audio(self, video_path: Path, workdir: Path) -> Path: ...

    def extract_frame_diffs(self, video_path: Path) -> list[float]: ...

    def extract_frames(self, video_path: Path, workdir: Path) -> list[Path]: ...


class CommandRunner(Protocol):
    def run(self, command: list[str]) -> None: ...


@dataclass
class OrchestratorDeps:
    store: PipelineStore
    media: MediaProbe
    transcriber: TranscriptionClient
    vision: VisionClient
    push: PushClient
    runner: CommandRunner
    workdir: Path
    target_duration_sec: int = 90
    vintage: bool = False


@dataclass
class NightlyRunResult:
    devices_processed: int = 0
    devices_failed: list[str] | None = None

    def __post_init__(self) -> None:
        if self.devices_failed is None:
            self.devices_failed = []


def run_nightly(deps: OrchestratorDeps, day: date) -> NightlyRunResult:
    result = NightlyRunResult()
    for device in deps.store.list_devices():
        try:
            _run_device_day(deps, device, day)
            result.devices_processed += 1
        except Exception:
            logger.exception("nightly run failed for device %s", device.device_id)
            result.devices_failed.append(device.device_id)
    return result


def _run_device_day(deps: OrchestratorDeps, device: DeviceRecord, day: date) -> None:
    store = deps.store
    pending = store.load_pending_dlq(device.device_id)
    pending_by_key = {entry.key: entry for entry in pending}

    segment_keys = store.list_segment_object_keys(device.device_id, day)
    marker_keys = store.list_flag_marker_keys(device.device_id, day)
    retried_segment_keys = [e.key for e in pending if e.kind == "rejected"]
    retried_marker_keys = [e.key for e in pending if e.kind == "unmatched"]

    segments, rejected = build_segments_from_object_keys(
        segment_keys + retried_segment_keys, device.device_id
    )
    segments, marker_rejected, unmatched = apply_flag_markers(
        segments, marker_keys + retried_marker_keys, device.device_id
    )
    unmatched = [
        key
        for key in unmatched
        if not _flag_persisted_segment(store, device.device_id, key)
    ]
    failed_keys = {key: "rejected" for key in rejected}
    failed_keys.update((key, "rejected") for key in marker_rejected)
    failed_keys.update((key, "unmatched") for key in unmatched)
    recovered = _apply_dlq_policy(deps, device, pending_by_key, failed_keys)

    _process_device_segments(deps, device, day, segments)

    if recovered:
        store.resolve_dlq(device.device_id, recovered)


def _flag_persisted_segment(
    store: PipelineStore, device_id: str, marker_key: str
) -> bool:
    flag_at = parse_flag_marker_filename(marker_key.split("/")[-1])
    for row in store.list_segment_rows(device_id, flag_at.date()):
        recorded_at = datetime.fromisoformat(row["recorded_at"]).replace(tzinfo=None)
        window_end = recorded_at + timedelta(seconds=row["duration_sec"])
        if recorded_at <= flag_at < window_end:
            store.flag_segment(row["s3_key"])
            return True
    return False


def _process_device_segments(
    deps: OrchestratorDeps, device: DeviceRecord, day: date, segments: list[Segment]
) -> None:
    store = deps.store
    if not segments:
        return

    weights = store.fetch_score_weights(device.user_id)
    device_workdir = deps.workdir / device.device_id
    device_workdir.mkdir(parents=True, exist_ok=True)
    scored = [
        _score_one(deps, segment, weights, device_workdir) for segment in segments
    ]
    store.persist_segments([_segment_row(s) for s in scored])

    selected = select_highlights(scored, target_duration_sec=deps.target_duration_sec)
    if not selected:
        return
    reel_path = _assemble_reel(deps, selected, device_workdir, day)

    reel_key = store.upload_reel(device.device_id, day, reel_path)
    store.insert_reel(
        {
            "device_id": device.device_id,
            "date": day.isoformat(),
            "s3_key": reel_key,
            "duration_sec": len(selected) * CLIP_DURATION_SEC,
            "style": "vintage" if deps.vintage else "clean",
        }
    )
    if device.push_token:
        _deliver_push(
            deps, device, lambda: notify_reel_ready(deps.push, device.push_token, day)
        )


def _deliver_push(deps: OrchestratorDeps, device: DeviceRecord, send) -> bool:
    """Attempt delivery; True when delivered or permanently undeliverable."""
    try:
        send()
        return True
    except PushTokenNotRegisteredError:
        logger.warning(
            "push token for device %s is no longer registered; clearing it",
            device.device_id,
        )
        deps.store.clear_push_token(device.device_id)
        device.push_token = None
        return True
    except Exception:
        logger.exception("push delivery failed for device %s", device.device_id)
        return False


def _apply_dlq_policy(
    deps: OrchestratorDeps,
    device: DeviceRecord,
    pending_by_key: dict[str, DlqEntry],
    failed_keys: dict[str, str],
) -> list[str]:
    store = deps.store
    recovered = [key for key in pending_by_key if key not in failed_keys]

    to_record: list[DlqEntry] = []
    to_escalate: list[str] = []
    for key, kind in failed_keys.items():
        attempts = pending_by_key[key].attempts + 1 if key in pending_by_key else 1
        if attempts >= MAX_DLQ_ATTEMPTS:
            to_escalate.append(key)
        else:
            to_record.append(DlqEntry(key=key, kind=kind, attempts=attempts))

    if to_record:
        store.record_dlq(device.device_id, to_record)
    if to_escalate:
        notified = True
        if device.push_token:
            notified = _deliver_push(
                deps,
                device,
                lambda: deps.push.send(
                    device.push_token, ESCALATION_TITLE, ESCALATION_BODY
                ),
            )
        if notified:
            store.escalate_dlq(device.device_id, to_escalate)
    return recovered


def _score_one(
    deps: OrchestratorDeps, segment: Segment, weights: ScoreWeights, workdir: Path
) -> Segment:
    video_path = deps.store.download_segment(segment.s3_key, workdir)
    audio_path = deps.media.extract_audio(video_path, workdir)
    transcription = deps.transcriber.transcribe(audio_path)
    frame_diffs = deps.media.extract_frame_diffs(video_path)
    frame_paths = deps.media.extract_frames(video_path, workdir)
    segment.composite_score, segment.location = score_segment(
        transcription=transcription,
        frame_diffs=frame_diffs,
        vision_client=deps.vision,
        frame_paths=frame_paths,
        weights=weights,
        manually_flagged=segment.manually_flagged,
    )
    return segment


def _segment_row(segment: Segment) -> dict:
    return {
        "device_id": segment.device_id,
        "recorded_at": segment.recorded_at.isoformat(),
        "duration_sec": segment.duration_sec,
        "s3_key": segment.s3_key,
        "composite_score": segment.composite_score,
        "manually_flagged": segment.manually_flagged,
    }


def _assemble_reel(
    deps: OrchestratorDeps, selected: list[Segment], workdir: Path, day: date
) -> Path:
    clip_paths: list[Path] = []
    for index, segment in enumerate(selected):
        source = workdir / Path(segment.s3_key).name
        clip = workdir / f"clip_{index:03d}.mp4"
        offset = compute_clip_offset_sec(segment.duration_sec, CLIP_DURATION_SEC)
        deps.runner.run(build_trim_command(source, clip, offset, CLIP_DURATION_SEC))
        clip_paths.append(clip)

    concat_file = workdir / "concat.txt"
    concat_file.write_text(render_concat_file_content(clip_paths))
    reel_path = workdir / f"reel_{day.strftime('%Y%m%d')}.mp4"
    deps.runner.run(build_assembly_command(concat_file, reel_path, deps.vintage))
    return reel_path
