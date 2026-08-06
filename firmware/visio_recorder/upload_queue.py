import shutil
from pathlib import Path


def enqueue(queue_dir: Path, source_path: Path) -> Path:
    queue_dir.mkdir(parents=True, exist_ok=True)
    dest = queue_dir / source_path.name
    shutil.move(str(source_path), str(dest))
    return dest


def list_pending(queue_dir: Path) -> list[Path]:
    if not queue_dir.exists():
        return []
    return sorted(queue_dir.iterdir(), key=lambda p: p.name)


def mark_uploaded(path: Path) -> None:
    # Idempotent because queue_dir has two concurrent drainers: FlagUploadWorker
    # on its own thread, and flush_pending on the upload worker thread after
    # every successful segment. Either can upload and remove a file in the
    # window between the other's upload and its mark_uploaded, and both call
    # this outside their try. "No longer queued" is the postcondition callers
    # want, matching the idempotent upload the x-upsert header already gives.
    path.unlink(missing_ok=True)
