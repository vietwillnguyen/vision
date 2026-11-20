"""Never let a segment file be retried forever because it is too large.

Storage rejects any object over its per-file limit (50 MiB on the hosted free
plan) with a 413, and a rejected file stays queued, so without this guard an
oversized segment was retried on every boot and never uploaded. Segment length
is chosen so files fit (``visio_recorder.segment_size``), but the Pi's encoder
can overshoot its target bitrate, and a limit lowered on the server side makes
a fitting file too large after the fact. So before upload:

- a segment over ``max_bytes`` is split losslessly (``ffmpeg -c copy -f
  segment``) into parts targeting 80 % of the limit, each named after its own
  start time (``YYYYMMDD_HHMMSS.mp4``) so the pipeline treats them as ordinary,
  consecutive segments;
- a part storage still answers 413 for is split again, under half its size;
- a segment that cannot be split under the limit (ffprobe cannot read its
  duration, ffmpeg fails with at least twice its size free, the split yields
  fewer than two parts, a part is still too large, or parts would share a
  name) is moved to ``rejected/`` beside the queue, logged as an error and
  counted in ``device_status.segments_rejected``. It is kept for manual
  recovery but never retried. A file storage already holds under a key this
  login cannot read (stored under an earlier pairing, answered 409) is set
  aside the same way;
- a split that fails for a passing reason (any ``OSError`` such as a full SD
  card, or ffmpeg failing while the queue's filesystem has less free space
  than twice the file being split, which covers the parts, their per-part
  ``moov`` overhead and the segment still recording) leaves the segment
  queued and counts it as failed, so the upload worker's next in-process
  retry round tries again, with the next boot's flush as the backstop.

Splitting is crash-safe: parts are built in a scratch directory inside the
queue (skipped by ``list_pending``), every part except the one that reuses the
original's name is moved into place first, and the original is replaced last,
so a power cut at any point leaves the original to be split again on the next
boot. Re-splitting the same file produces the same parts, which the storage
adapter's size check recognises as already uploaded.
"""

import logging
import math
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from visio_recorder.muxer import CommandRunner
from visio_recorder.upload_queue import mark_uploaded
from visio_recorder.uploader import (
    LoginUnavailableError,
    ObjectAlreadyStoredError,
    ObjectTooLargeError,
    StorageClient,
    upload_segment,
)

# Parts aim for this fraction of the limit: -c copy can only cut at keyframes,
# so a part runs up to one GOP (2 s at the encoder's default intra period)
# past its target length.
SPLIT_FILL = 0.8
_STEM_FORMAT = "%Y%m%d_%H%M%S"

_logger = logging.getLogger(__name__)


class SegmentSplitError(Exception):
    """The segment could not be split into parts under the limit."""


class SegmentSplitDeferred(Exception):
    """The split failed for a passing reason; the segment can be retried."""


class DurationProbe(Protocol):
    def duration_sec(self, path: Path) -> float: ...


class FfprobeDurationProbe:
    def duration_sec(self, path: Path) -> float:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return float(completed.stdout.strip())


@dataclass
class UploadLimit:
    max_bytes: int
    runner: CommandRunner
    probe: DurationProbe


@dataclass
class UploadOutcome:
    uploaded: int = 0
    failed: int = 0
    rejected: int = 0
    # Files left queued because the login could not sign the request (offline
    # when it fell due, or revoked): not a failure of the upload itself.
    deferred: int = 0
    # Queue entries an upload was actually tried for, which a drain cancelled
    # before its first file, or given nothing to drain, leaves at zero.
    attempted: int = 0


def rejected_dir(queue_dir: Path) -> Path:
    return queue_dir.parent / "rejected"


def build_split_command(source: Path, chunk_sec: float, work_dir: Path) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-map",
        "0",
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        f"{chunk_sec:.3f}".rstrip("0").rstrip("."),
        "-segment_format",
        "mp4",
        "-reset_timestamps",
        "1",
        "-segment_list",
        str(work_dir / "parts.csv"),
        "-segment_list_type",
        "csv",
        str(work_dir / "part_%03d.mp4"),
    ]


def split_segment(
    path: Path, max_bytes: int, runner: CommandRunner, probe: DurationProbe
) -> list[Path]:
    """Replace an oversized segment with parts of at most ``max_bytes`` each.

    Returns ``[path]`` unchanged for a file within the limit or anything that
    is not a segment. Raises ``SegmentSplitError`` (leaving ``path`` intact)
    when the parts cannot be made to fit, and ``SegmentSplitDeferred`` (also
    leaving ``path`` intact) when the split failed for a passing reason.
    """
    size = path.stat().st_size
    if size <= max_bytes or path.suffix != ".mp4":
        return [path]
    started_at = datetime.strptime(path.stem, _STEM_FORMAT)
    work_dir = path.parent / f".{path.stem}.split"
    shutil.rmtree(work_dir, ignore_errors=True)
    try:
        work_dir.mkdir()
        chunks = math.ceil(size / (max_bytes * SPLIT_FILL))
        chunk_sec = probe.duration_sec(path) / chunks
        try:
            runner.run(build_split_command(path, chunk_sec, work_dir))
        except subprocess.CalledProcessError as err:
            free = shutil.disk_usage(path.parent).free
            if free < 2 * size:
                raise SegmentSplitDeferred(
                    f"{path.name}: ffmpeg failed with {free} bytes free: {err!r}"
                ) from err
            raise
        parts: list[tuple[Path, Path]] = []
        for line in (work_dir / "parts.csv").read_text().splitlines():
            if not line.strip():
                continue
            name, start, _end = line.split(",")
            part = work_dir / name
            offset = timedelta(seconds=math.floor(float(start)))
            final = path.parent / f"{(started_at + offset).strftime(_STEM_FORMAT)}.mp4"
            parts.append((part, final))
        if len(parts) < 2:
            raise SegmentSplitError(f"{path.name}: split produced {len(parts)} part")
        too_large = [p.name for p, _ in parts if p.stat().st_size > max_bytes]
        if too_large:
            raise SegmentSplitError(
                f"{path.name}: parts {too_large} are still over {max_bytes} bytes"
            )
        if len({final for _, final in parts}) != len(parts):
            raise SegmentSplitError(f"{path.name}: parts would share a name")
        # The original is replaced last so a power cut leaves it to re-split.
        for part, final in sorted(parts, key=lambda pf: pf[1] == path):
            part.replace(final)
    except (SegmentSplitError, SegmentSplitDeferred):
        raise
    except OSError as err:
        raise SegmentSplitDeferred(f"{path.name}: {err!r}") from err
    except Exception as err:
        raise SegmentSplitError(f"{path.name}: {err!r}") from err
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    if path not in {final for _, final in parts}:
        path.unlink()
    finals = sorted(final for _, final in parts)
    _logger.warning(
        "split %s (%d bytes, over the %d-byte upload limit) into %s",
        path.name,
        size,
        max_bytes,
        ", ".join(p.name for p in finals),
    )
    return finals


def _reject(path: Path, err: Exception) -> None:
    dest_dir = rejected_dir(path.parent)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest_dir / path.name))
    _logger.error(
        "segment %s can never be uploaded (%s); moved to %s and will not be retried",
        path.name,
        err,
        dest_dir,
    )


def upload_queued(
    path: Path, storage: StorageClient, device_id: str, limit: UploadLimit
) -> UploadOutcome:
    """Upload one queued file, splitting it first if it is over the limit.

    Every uploaded file is removed from the queue; a file that failed for any
    other reason (network, auth) stays queued for the next in-process retry
    round, with the next boot's flush as the backstop. A file the login
    could not sign (``LoginUnavailableError``) stays queued too, counted as
    ``deferred`` rather than ``failed``. That holds for a flag
    marker too, except that its rounds belong to ``FlagUploadWorker``; this
    path only ever sees one from the startup flush, which runs before that
    worker exists.
    """
    outcome = UploadOutcome()
    if not path.exists():
        return outcome
    work: deque[tuple[Path, int]] = deque([(path, limit.max_bytes)])
    while work:
        current, max_bytes = work.popleft()
        try:
            parts = split_segment(current, max_bytes, limit.runner, limit.probe)
        except SegmentSplitError as err:
            _reject(current, err)
            outcome.rejected += 1
            continue
        except SegmentSplitDeferred as err:
            _logger.warning(
                "could not split %s yet (%s); keeping it queued", current.name, err
            )
            outcome.failed += 1
            continue
        for part in parts:
            try:
                upload_segment(storage, device_id, part)
            except ObjectTooLargeError as err:
                size = part.stat().st_size
                _logger.warning(
                    "storage rejected %s (%d bytes) as too large: %s; "
                    "splitting it under %d bytes",
                    part.name,
                    size,
                    err,
                    size // 2,
                )
                work.append((part, size // 2))
                continue
            except ObjectAlreadyStoredError as err:
                _reject(part, err)
                outcome.rejected += 1
                continue
            except LoginUnavailableError as err:
                _logger.warning("%s stays queued: %s", part.name, err)
                outcome.deferred += 1
                continue
            except Exception:
                _logger.exception("upload of %s failed; keeping it queued", part)
                outcome.failed += 1
                continue
            mark_uploaded(part)
            outcome.uploaded += 1
    return outcome
