import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from visio_recorder.upload_queue import mark_uploaded
from visio_recorder.uploader import StorageClient, upload_segment

FLAG_PRESS_COOLDOWN = timedelta(seconds=2)

_logger = logging.getLogger(__name__)


class FlagButton(Protocol):
    def on_press(self, callback: Callable[[], None]) -> None: ...


def write_flag_marker(queue_dir: Path, pressed_at: datetime) -> Path:
    queue_dir.mkdir(parents=True, exist_ok=True)
    marker_path = queue_dir / f"FLAG_{pressed_at.strftime('%Y%m%d_%H%M%S')}.marker"
    marker_path.touch()
    return marker_path


def make_flag_press_handler(
    queue_dir: Path,
    storage_client: StorageClient,
    device_id: str,
    clock: Callable[[], datetime] = datetime.now,
    cooldown: timedelta = FLAG_PRESS_COOLDOWN,
) -> Callable[[], None]:
    """Build the callback a button press invokes.

    Presses inside ``cooldown`` of the last accepted press are dropped (switch
    bounce and accidental double-taps; second-resolution marker names collide
    anyway). The marker is uploaded immediately so it reaches the pipeline
    before tonight's run; on upload failure it stays in ``queue_dir``, where
    the next boot's ``flush_pending`` retries it - the same retry contract as
    segments.
    """
    last_accepted: datetime | None = None

    def on_press() -> None:
        nonlocal last_accepted
        pressed_at = clock()
        if last_accepted is not None and pressed_at - last_accepted < cooldown:
            return
        last_accepted = pressed_at
        marker_path = write_flag_marker(queue_dir, pressed_at)
        try:
            upload_segment(storage_client, device_id, marker_path)
        except Exception:
            _logger.exception(
                "flag marker upload failed; %s stays queued for next-boot flush",
                marker_path,
            )
            return
        mark_uploaded(marker_path)

    return on_press
