"""The three gaps this change closes, run against the firmware before it.

Point ``VISIO_FIRMWARE`` at a checkout of the base commit's ``firmware/``
and this prints what a flag press did then. It imports only
``flag_button``, ``recording_loop`` and ``upload_queue``, none of which pull
in a third-party package, so it runs on a bare interpreter against a
``visio_recorder`` tree extracted with ``git archive``:

    git archive <base> firmware/visio_recorder \
        | tar -x -C /tmp/before --strip-components=1
    VISIO_FIRMWARE=/tmp/before python3 before_gaps.py
"""

import inspect
import logging
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

FIRMWARE = Path(
    os.environ.get("VISIO_FIRMWARE", Path(__file__).resolve().parents[4] / "firmware")
)
sys.path.insert(0, str(FIRMWARE))

from visio_recorder.flag_button import (  # noqa: E402
    FlagUploadWorker,
    make_flag_press_handler,
    write_flag_marker,
)
from visio_recorder.recording_loop import flush_pending  # noqa: E402
from visio_recorder.upload_limit import UploadLimit  # noqa: E402
from visio_recorder.upload_queue import list_pending  # noqa: E402

DEVICE = "device-sim"


class _NoTraceback(logging.Formatter):
    def formatException(self, exc_info):
        return ""

    def format(self, record):
        return "    firmware: " + record.getMessage()


class Down:
    """Storage with no network, recording every attempt it is given."""

    def __init__(self) -> None:
        self.attempts = []

    def upload(self, bucket, object_path, local_path):
        self.attempts.append(object_path)
        raise ConnectionError("network down")


class Up:
    def __init__(self) -> None:
        self.stored = []

    def upload(self, bucket, object_path, local_path):
        self.stored.append(object_path)


class _NoProbe:
    def duration_sec(self, path):
        raise AssertionError("nothing should need splitting")


def gap_1_no_in_process_retry() -> None:
    print("\n1. A failed marker waits for the next boot")
    print("-" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        queue_dir = Path(tmp) / "queue"
        storage = Down()
        worker = FlagUploadWorker(storage, DEVICE)
        worker.submit(write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, 30)))
        # Far longer than the 30 s base backoff a segment would be retried on.
        time.sleep(2.0)
        worker.stop()
        print(f"  upload attempts in 2 s of outage: {len(storage.attempts)}")
        print(f"  still queued: {[p.name for p in list_pending(queue_dir)]}")
        print("  -> one attempt, ever. The flag is stuck on the card until a reboot,")
        print("     and the nightly run scores that segment unflagged.")


def gap_2_segments_overtake_their_flags() -> None:
    print("\n2. The startup flush sends segments before their flags")
    print("-" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        queue_dir = Path(tmp) / "queue"
        queue_dir.mkdir(parents=True)
        for minute in range(4):
            (queue_dir / f"2026070412{minute:02d}00.mp4").write_bytes(b"segment")
        for stamp in ("20260704120030", "20260704120401"):
            write_flag_marker(queue_dir, datetime.strptime(stamp, "%Y%m%d%H%M%S"))

        class DiesAfter(Up):
            def __init__(self, budget):
                super().__init__()
                self.budget = budget

            def upload(self, bucket, object_path, local_path):
                if self.budget <= 0:
                    raise ConnectionError("battery gave out")
                self.budget -= 1
                super().upload(bucket, object_path, local_path)

        storage = DiesAfter(budget=3)
        flush_pending(
            queue_dir,
            storage,
            DEVICE,
            UploadLimit(max_bytes=2**40, runner=None, probe=_NoProbe()),
        )
        print("  reached storage before the power went:")
        for key in storage.stored:
            print(f"    {key}")
        print(f"  still on the card: {[p.name for p in list_pending(queue_dir)]}")
        print("  -> three segments up, neither flag. Name order puts digits before")
        print("     'FLAG_', so the footage always overtakes the flags.")


def gap_3_no_acknowledgement() -> None:
    print("\n3. A press has no local feedback at all")
    print("-" * 70)
    params = list(inspect.signature(make_flag_press_handler).parameters)
    print(f"  make_flag_press_handler parameters: {params}")
    source = inspect.getsource(write_flag_marker)
    print(f"  write_flag_marker syncs anything: {'fsync' in source}")
    print("  -> nothing to acknowledge with, and the marker is only as durable")
    print("     as the page cache: touch() and return.")


def main() -> int:
    print(f"visio flag marker, BEFORE - firmware at {FIRMWARE}")
    threading.current_thread().name = "main"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_NoTraceback())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    gap_1_no_in_process_retry()
    gap_2_segments_overtake_their_flags()
    gap_3_no_acknowledgement()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
