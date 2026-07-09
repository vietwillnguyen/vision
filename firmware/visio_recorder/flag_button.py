from datetime import datetime
from pathlib import Path


def write_flag_marker(queue_dir: Path, pressed_at: datetime) -> Path:
    queue_dir.mkdir(parents=True, exist_ok=True)
    marker_path = queue_dir / f"FLAG_{pressed_at.strftime('%Y%m%d_%H%M%S')}.marker"
    marker_path.touch()
    return marker_path
