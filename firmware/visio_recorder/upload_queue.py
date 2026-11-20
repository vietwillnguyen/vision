import shutil
from pathlib import Path

MARKER_SUFFIX = ".marker"


def enqueue(queue_dir: Path, source_path: Path) -> Path:
    queue_dir.mkdir(parents=True, exist_ok=True)
    dest = queue_dir / source_path.name
    shutil.move(str(source_path), str(dest))
    return dest


def list_pending(queue_dir: Path) -> list[Path]:
    if not queue_dir.exists():
        return []
    # Skips the hidden scratch directory a split works in (upload_limit.py).
    return sorted(
        (p for p in queue_dir.iterdir() if p.is_file() and not p.name.startswith(".")),
        key=lambda p: p.name,
    )


def is_flag_marker(path: Path) -> bool:
    return path.suffix == MARKER_SUFFIX


def pending_markers(queue_dir: Path) -> list[Path]:
    """The flag markers still queued, oldest first.

    ``FlagUploadWorker`` owns every one of these while the daemon runs: it
    uploads them as they are pressed and retries the rest in process, and
    ``pending_segments`` leaves them out so the upload worker's own retry
    rounds never touch one.
    """
    return [path for path in list_pending(queue_dir) if is_flag_marker(path)]


def markers_before_segments(paths: list[Path]) -> list[Path]:
    """``paths`` reordered so every flag marker is uploaded before any segment.

    The pipeline scores a segment against the markers that are in storage
    when it runs, so a drain that stops part-way - a reboot, a battery halt,
    WiFi going again - must not leave a segment's flags behind it. Plain name
    order is exactly backwards for that: digits sort before ``FLAG_``, so the
    segments would go first.

    This is coarser than the pipeline's matching window on purpose. Working
    out which segment each marker belongs to would need every segment's real
    duration; sending all of them first costs nothing extra, and covers both
    a marker inside a segment and one in the 3 s restart gap that the
    pipeline attaches to the segment that just ended. Within each kind the
    order is by name, which for both is time order.
    """
    ordered = sorted(paths, key=lambda p: p.name)
    return [p for p in ordered if is_flag_marker(p)] + [
        p for p in ordered if not is_flag_marker(p)
    ]


def mark_uploaded(path: Path) -> None:
    path.unlink()
