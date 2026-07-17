import logging
import queue
import threading
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


class FlagUploadWorker:
    """Owns every flag-marker upload on a single dedicated thread.

    Press callbacks run on gpiozero's internal callback thread, which must
    never block on the network (a slow upload would delay detection of later
    presses), so they only hand the marker path over via ``submit``. The
    marker is still uploaded immediately - as soon as this thread picks it
    up - so it reaches the pipeline before tonight's run. On upload failure
    the marker stays in the queue dir, where the next boot's
    ``flush_pending`` retries it - the same retry contract as segments.
    """

    def __init__(self, storage_client: StorageClient, device_id: str) -> None:
        self._storage_client = storage_client
        self._device_id = device_id
        self._work: "queue.Queue[Path | None]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, marker_path: Path) -> None:
        self._work.put(marker_path)

    def stop(self) -> None:
        """Drain queued markers, then join the worker thread."""
        self._work.put(None)
        self._thread.join()

    def _run(self) -> None:
        while True:
            item = self._work.get()
            if item is None:
                return
            try:
                upload_segment(self._storage_client, self._device_id, item)
            except Exception:
                _logger.exception(
                    "flag marker upload failed; %s stays queued for "
                    "next-boot flush",
                    item,
                )
                continue
            mark_uploaded(item)


def make_flag_press_handler(
    queue_dir: Path,
    submit: Callable[[Path], None],
    clock: Callable[[], datetime] = datetime.now,
    cooldown: timedelta = FLAG_PRESS_COOLDOWN,
) -> Callable[[], None]:
    """Build the callback a button press invokes.

    Presses inside ``cooldown`` of the last accepted press are dropped (switch
    bounce and accidental double-taps; second-resolution marker names collide
    anyway). The callback only debounces, writes the marker, and hands the
    path to ``submit`` (the flag-upload worker); it never touches the network
    itself, so gpiozero's callback thread stays responsive.
    """
    last_accepted: datetime | None = None

    def on_press() -> None:
        nonlocal last_accepted
        pressed_at = clock()
        if last_accepted is not None and pressed_at - last_accepted < cooldown:
            return
        last_accepted = pressed_at
        marker_path = write_flag_marker(queue_dir, pressed_at)
        submit(marker_path)

    return on_press
