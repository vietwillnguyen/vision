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
    path.unlink()
