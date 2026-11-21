"""Turn ordinary video clips into a pendant-shaped device day.

The demo has to run the real nightly pipeline on footage that looks exactly
like what the firmware uploads, so each clip is cut into the firmware's
one-minute segments, re-encoded to its capture format (1280x720 H.264 at
30 fps, capped at its 4 Mbps bitrate, mono AAC when there is sound) and then
muxed with the very command ``firmware/visio_recorder/muxer.py`` runs.
``integration/tests/test_demo_firmware_contract.py`` holds the two muxing
commands equal.

Segments are named ``YYYYMMDD_HHMMSS.mp4`` and flag presses become
``FLAG_YYYYMMDD_HHMMSS.marker`` files, the names the pipeline parses. Like
``rpicam-vid``'s per-segment restarts, each segment of a clip starts one
second after the previous one ended.
"""

import math
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol

from pipeline.adapters.ffmpeg_media import (
    FfmpegMediaProbe,
    build_audio_stream_probe_command,
    run_captured,
)
from pipeline.demo.manifest import MANIFEST_NAME, Manifest, parse_manifest

FIRMWARE_WIDTH = 1280
FIRMWARE_HEIGHT = 720
FIRMWARE_FRAMERATE = 30
FIRMWARE_VIDEO_BITRATE = 4_000_000
FIRMWARE_AUDIO_BITRATE = 64_000
FIRMWARE_AUDIO_SAMPLE_RATE = 48_000
SEGMENT_SEC = 60
RESTART_GAP_SEC = 1
# A remainder this short is dropped: it would be a segment of a few frames.
MIN_SEGMENT_SEC = 1.0
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".webm"})


class CommandRunner(Protocol):
    def run(self, command: list[str]) -> None: ...


@dataclass(frozen=True)
class SourceClip:
    path: Path
    duration_sec: float
    has_audio: bool
    start: time | None = None
    flags: tuple[float, ...] = ()


@dataclass(frozen=True)
class PlannedSegment:
    source: Path
    offset_sec: float
    duration_sec: float
    recorded_at: datetime
    has_audio: bool

    @property
    def filename(self) -> str:
        return f"{self.recorded_at:%Y%m%d_%H%M%S}.mp4"


@dataclass(frozen=True)
class DayPlan:
    segments: list[PlannedSegment]
    markers: list[str]


def flag_marker_name(at: datetime) -> str:
    return f"FLAG_{at:%Y%m%d_%H%M%S}.marker"


def plan_day(
    clips: list[SourceClip],
    day: date,
    *,
    default_start: time,
    gap_sec: int = 7 * 60,
    segment_sec: int = SEGMENT_SEC,
) -> DayPlan:
    """Place the clips on ``day`` and cut them into firmware segments."""
    segments: list[PlannedSegment] = []
    markers: list[str] = []
    day_end = datetime.combine(day + timedelta(days=1), time())
    previous_end: datetime | None = None

    for clip in clips:
        if clip.start is not None:
            clip_start = datetime.combine(day, clip.start)
            if previous_end is not None and clip_start < previous_end:
                raise ValueError(
                    f"{clip.path.name} starts at {clip.start} but the clip before "
                    f"it is still recording until {previous_end:%H:%M:%S}"
                )
        elif previous_end is None:
            clip_start = datetime.combine(day, default_start)
        else:
            clip_start = previous_end + timedelta(seconds=gap_sec)

        pieces = _cut(clip, clip_start, segment_sec)
        if not pieces:
            raise ValueError(f"{clip.path.name} is too short to make a segment")
        for flag in clip.flags:
            markers.append(flag_marker_name(_flag_time(clip, flag, pieces)))

        last = pieces[-1]
        previous_end = _ceil_second(
            last.recorded_at + timedelta(seconds=last.duration_sec)
        )
        if previous_end > day_end:
            raise ValueError(
                f"{clip.path.name} runs past midnight; start the day earlier"
            )
        segments.extend(pieces)

    return DayPlan(segments=segments, markers=markers)


def _cut(
    clip: SourceClip, clip_start: datetime, segment_sec: int
) -> list[PlannedSegment]:
    pieces = []
    index = 0
    while index * segment_sec < clip.duration_sec:
        offset = index * segment_sec
        duration = min(segment_sec, clip.duration_sec - offset)
        if duration >= MIN_SEGMENT_SEC:
            pieces.append(
                PlannedSegment(
                    source=clip.path,
                    offset_sec=offset,
                    duration_sec=duration,
                    recorded_at=clip_start
                    + timedelta(seconds=index * (segment_sec + RESTART_GAP_SEC)),
                    has_audio=clip.has_audio,
                )
            )
        index += 1
    return pieces


def _flag_time(clip: SourceClip, flag: float, pieces: list[PlannedSegment]) -> datetime:
    for piece in pieces:
        if piece.offset_sec <= flag < piece.offset_sec + piece.duration_sec:
            at = piece.recorded_at + timedelta(seconds=flag - piece.offset_sec)
            # Marker names have whole seconds; flooring keeps the press inside
            # its segment, which always starts on a whole second.
            return at.replace(microsecond=0)
    raise ValueError(
        f"{clip.path.name}: flag at {flag:g}s is outside the clip's "
        f"{clip.duration_sec:g}s of footage"
    )


def _ceil_second(at: datetime) -> datetime:
    if at.microsecond:
        return at.replace(microsecond=0) + timedelta(seconds=1)
    return at


def _seconds(value: float) -> str:
    return f"{value:g}" if value == int(value) else f"{value:.3f}"


def capture_command(segment: PlannedSegment, capture: Path) -> list[str]:
    """Re-encode one segment's slice of its clip to what ``rpicam-vid`` writes.

    Video only, it is a raw ``.h264`` elementary stream; with sound it is
    MPEG-TS carrying H.264 and AAC, as rpicam-vid's libav backend writes it.
    The picture is scaled to fill 1280x720 and centre-cropped, as the
    pendant's camera has no letterboxing, and the bitrate is capped at the
    firmware's 4 Mbps so a one-minute segment always fits the 50 MiB bucket.
    """
    fill = (
        f"scale={FIRMWARE_WIDTH}:{FIRMWARE_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={FIRMWARE_WIDTH}:{FIRMWARE_HEIGHT},setsar=1"
    )
    command = [
        "ffmpeg", "-v", "error", "-y",
        "-ss", _seconds(segment.offset_sec), "-i", str(segment.source),
        "-t", _seconds(segment.duration_sec),
        "-vf", fill, "-r", str(FIRMWARE_FRAMERATE),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-crf", "23",
        "-maxrate", str(FIRMWARE_VIDEO_BITRATE),
        "-bufsize", str(2 * FIRMWARE_VIDEO_BITRATE),
        "-g", str(FIRMWARE_FRAMERATE),
    ]  # fmt: skip
    if segment.has_audio:
        command += [
            "-c:a", "aac", "-ac", "1",
            "-ar", str(FIRMWARE_AUDIO_SAMPLE_RATE),
            "-b:a", str(FIRMWARE_AUDIO_BITRATE),
            "-f", "mpegts",
        ]  # fmt: skip
    else:
        command += ["-an", "-f", "h264"]
    return [*command, str(capture)]


def mux_command(capture: Path, framerate: int) -> list[str]:
    """The firmware's own muxing command; see ``muxer.mux_segment``."""
    mp4_path = capture.with_suffix(".mp4")
    if capture.suffix == ".ts":
        return ["ffmpeg", "-y", "-i", str(capture), "-map", "0", "-c", "copy",
                str(mp4_path)]  # fmt: skip
    return ["ffmpeg", "-y", "-framerate", str(framerate), "-i", str(capture),
            "-c", "copy", str(mp4_path)]  # fmt: skip


def render_day(runner: CommandRunner, plan: DayPlan, out_dir: Path) -> list[Path]:
    """Render every planned segment into ``out_dir``; returns the MP4 paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered = []
    for segment in plan.segments:
        stem = segment.filename.removesuffix(".mp4")
        capture = out_dir / f"{stem}{'.ts' if segment.has_audio else '.h264'}"
        runner.run(capture_command(segment, capture))
        runner.run(mux_command(capture, FIRMWARE_FRAMERATE))
        capture.unlink()
        rendered.append(capture.with_suffix(".mp4"))
    return rendered


def find_source_clips(directory: Path) -> list[Path]:
    return sorted(
        (
            p
            for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        ),
        key=lambda p: p.name,
    )


def load_source_clips(directory: Path) -> tuple[list[SourceClip], Manifest]:
    """Probe every clip in ``directory`` and apply its ``demo.toml``, if any."""
    paths = find_source_clips(directory)
    if not paths:
        raise ValueError(
            f"{directory} has no video clips ({', '.join(sorted(VIDEO_EXTENSIONS))})"
        )
    manifest_path = directory / MANIFEST_NAME
    manifest = (
        parse_manifest(manifest_path.read_text())
        if manifest_path.exists()
        else Manifest()
    )
    names = {p.name for p in paths}
    unknown = sorted(set(manifest.clips) - names)
    if unknown:
        raise ValueError(
            f"{MANIFEST_NAME} lists clips that are not in {directory}: "
            + ", ".join(unknown)
        )

    probe = FfmpegMediaProbe()
    clips = []
    for path in paths:
        settings = manifest.clips.get(path.name)
        clip = SourceClip(
            path=path,
            duration_sec=probe.probe_duration_sec(path),
            has_audio=bool(
                run_captured(build_audio_stream_probe_command(path)).stdout.strip()
            ),
        )
        if settings is not None:
            clip = replace(clip, start=settings.start, flags=settings.flags)
        clips.append(clip)
    return clips, manifest


def total_seconds(plan: DayPlan) -> float:
    return math.fsum(s.duration_sec for s in plan.segments)
