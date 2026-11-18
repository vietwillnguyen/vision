"""Battery traces through the real firmware, old policy beside new.

Two parts, both simulation only - no Pi, no PiJuice, no Supabase, and nothing
that could reach a real ``systemctl poweroff``.

Part 1 replays recorded-shape battery traces through two deciders over the
same samples: ``OLD_POLICY``, which is verbatim what ``read_battery_status``
did before this change (``pct < 10`` on one sample, no voltage, no notion of
a charger), and the real ``BatteryMonitor``. It prints where the two diverge,
which is the whole case for the change.

Part 2 runs the real ``run_recording_loop`` end to end on five of those
traces, with the camera and ffmpeg faked by the firmware test suite's own
``FakeCommandRunner`` and the power-off behind ``FakePowerController``. It
prints the LED colours applied, the segments uploaded, the final status row,
and whether the device powered off and at what wake level.

Run from firmware/:

    uv run --locked --extra dev python \
        ../docs/superpowers/evidence/battery-hardening/e2e_battery_traces.py
"""

import logging
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

FIRMWARE = Path(__file__).resolve().parents[4] / "firmware"
sys.path.insert(0, str(FIRMWARE))

from tests.fakes import (  # noqa: E402
    FakeClock,
    FakeCommandRunner,
    FakeDiskStatsReader,
    FakeLedDriver,
    FakePowerController,
    FakeStatusClient,
    FakeStorageClient,
    unlimited_upload_limit,
)
from visio_recorder.battery import (  # noqa: E402
    BATTERY_FLOOR_MV,
    BATTERY_HALT_PCT,
    BatteryMonitor,
    BatterySample,
    BatteryState,
)
from visio_recorder.capture import VideoSettings  # noqa: E402
from visio_recorder.daemon import LoopDeps, run_recording_loop  # noqa: E402
from visio_recorder.led import LedPattern  # noqa: E402
from visio_recorder.recording_loop import DiskStats  # noqa: E402

LED_NAMES = {
    ((0, 255, 0), LedPattern.SOLID): "green",
    ((0, 0, 255), LedPattern.PULSING): "blue",
    ((255, 255, 0), LedPattern.PULSING): "yellow",
    ((255, 0, 0), LedPattern.FLASHING): "RED",
}


def old_policy(sample: BatterySample) -> bool:
    """What the firmware did before this change, verbatim.

    ``read_battery_status``: one reading, one comparison. No average, no
    confirmations, no voltage, and no idea whether a charger is connected.
    """
    return sample.pct < BATTERY_HALT_PCT


# Each trace is (pct, voltage_mv or None, external_power) per sample, one
# sample per recording-loop iteration - so one per segment, a minute apart at
# the firmware's default segment length.
# ``must_halt`` is the ground truth for the trace: what a correct firmware has
# to do with it. It is what turns this script from a printout into a check.
TRACES: dict[str, tuple[str, bool, list[tuple[int, int | None, bool]]]] = {
    "noisy-gauge-healthy-pack": (
        "A half-full pack under a camera encode and WiFi bursts. The gauge "
        "spikes down and recovers; nothing here is a dying battery.",
        False,
        [
            (45, 3850, False),
            (8, 3840, False),
            (44, 3845, False),
            (6, 3830, False),
            (43, 3840, False),
            (9, 3820, False),
            (42, 3835, False),
        ],
    ),
    "genuine-depletion": (
        "The real thing: a pack that keeps falling and does not come back.",
        True,
        [
            (20, 3700, False),
            (18, 3680, False),
            (16, 3660, False),
            (14, 3640, False),
            (12, 3610, False),
            (10, 3590, False),
            (8, 3560, False),
            (6, 3530, False),
            (5, 3500, False),
            (4, 3470, False),
            (3, 3440, False),
            (2, 3410, False),
        ],
    ),
    "sagging-voltage-under-load": (
        "The case the percentage alone cannot catch. The gauge still reads "
        "half full - it is resistance-compensated and lags - while the cell's "
        "terminal voltage has already collapsed under load.",
        True,
        [
            (55, 3700, False),
            (54, 3500, False),
            (54, 3280, False),
            (53, 3260, False),
            (53, 3240, False),
        ],
    ),
    "transmit-burst-sag": (
        "The same shape, but a single sag during one WiFi transmit burst. "
        "The rail comes straight back; halting on it would be wrong.",
        False,
        [
            (55, 3700, False),
            (54, 3240, False),
            (54, 3690, False),
            (53, 3700, False),
            (53, 3250, False),
            (53, 3680, False),
        ],
    ),
    "flat-but-charging": (
        "A pendant plugged in at 4 %. It is low, and it is recovering; "
        "nothing about it should stop recording.",
        False,
        [
            (4, 3450, True),
            (5, 3600, True),
            (8, 3700, True),
            (14, 3760, True),
            (22, 3800, True),
            (27, 3840, True),
        ],
    ),
    "hovering-at-the-warn-line": (
        "Charge drifting across the 20 % warning threshold all afternoon. "
        "What the 20-25 % hysteresis band exists to stop flickering.",
        False,
        [
            (22, 3720, False),
            (19, 3700, False),
            (21, 3710, False),
            (18, 3690, False),
            (22, 3700, False),
            (24, 3710, False),
            (26, 3730, False),
        ],
    ),
    "halt-then-charger-arrives": (
        "The rail collapses and the halt is confirmed, then somebody plugs "
        "the pendant in while its queue is still draining.",
        True,
        [
            (55, 3700, False),
            (54, 3250, False),
            (53, 3240, False),
            (52, 3800, True),
            (54, 3850, True),
        ],
    ),
    "depleting-then-plugged-in": (
        "Falling towards the halt threshold, then a charger arrives.",
        False,
        [
            (14, 3620, False),
            (11, 3580, False),
            (8, 3540, False),
            (7, 3520, True),
            (12, 3700, True),
            (20, 3780, True),
        ],
    ),
}


def rule(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def replay(name: str) -> tuple[int | None, int | None, bool]:
    """Print the trace through both policies; return where each first halts."""
    description, must_halt, trace = TRACES[name]
    monitor = BatteryMonitor()
    print()
    print(f"--- {name}")
    for line in description.split(". "):
        if line:
            print(f"    {line.rstrip('.')}.")
    print(
        "    Ground truth: this trace "
        + ("MUST halt." if must_halt else "must NOT halt.")
    )
    print()
    print(
        f"    {'#':>2}  {'pct':>4} {'mV':>6} {'ext':>4} | "
        f"{'avg':>4} {'low':>5} {'new':<16} {'old':<5} verdict"
    )
    new_at: int | None = None
    old_at: int | None = None
    for index, (pct, voltage_mv, external) in enumerate(trace, start=1):
        sample = BatterySample(
            pct=pct, voltage_mv=voltage_mv, external_power=external
        )
        verdict = monitor.observe(sample)
        if verdict.should_halt and new_at is None:
            new_at = index
        if old_policy(sample) and old_at is None:
            old_at = index
        new_text = (
            f"HALT ({verdict.halt_reason.value})" if verdict.should_halt else "-"
        )
        old_text = "HALT" if old_policy(sample) else "-"
        note = ""
        if verdict.should_halt is not old_policy(sample):
            note = (
                "the old policy would stop a pendant that must keep going"
                if old_policy(sample) and not must_halt
                else "the new policy catches what the old one cannot see"
                if verdict.should_halt and old_at is None
                else "the two disagree"
            )
        print(
            f"    {index:>2}  {pct:>4} {str(voltage_mv or '-'):>6} "
            f"{('yes' if external else 'no'):>4} | "
            f"{verdict.smoothed_pct:>4} {str(verdict.is_low):>5} "
            f"{new_text:<16} {old_text:<5} {note}"
        )
    new_ok = (new_at is not None) is must_halt
    old_ok = (old_at is not None) is must_halt
    print(
        f"    => new: {_at(new_at)} ({'correct' if new_ok else 'WRONG'})"
        f"   old: {_at(old_at)} ({'correct' if old_ok else 'WRONG'})"
    )
    return new_at, old_at, new_ok


def _at(index: int | None) -> str:
    return "never halts" if index is None else f"halts at sample {index}"


def run_loop(name: str) -> None:
    """Drive the real recording loop over a trace and report what it did."""
    description, _must_halt, trace = TRACES[name]

    class TraceReader:
        """A battery whose reading advances once per loop iteration."""

        def __init__(self) -> None:
            self.index = -1

        def _at(self) -> tuple[int, int | None, bool]:
            return trace[min(self.index, len(trace) - 1)]

        def get_charge_pct(self) -> int:
            self.index += 1
            return self._at()[0]

        def get_battery_voltage_mv(self) -> int:
            return self._at()[1] or 0

        def is_externally_powered(self) -> bool:
            return self._at()[2]

    led = FakeLedDriver()
    storage = FakeStorageClient()
    status = FakeStatusClient()
    power = FakePowerController()
    stop = threading.Event()
    # Stop after the trace is spent, so a trace that never halts still ends.
    runner = FakeCommandRunner(
        on_record=lambda count: stop.set() if count >= len(trace) else None
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        deps = LoopDeps(
            command_runner=runner,
            storage_client=storage,
            status_client=status,
            led_driver=led,
            battery=BatteryState(TraceReader()),
            power_controller=power,
            disk_stats_reader=FakeDiskStatsReader(
                DiskStats(used_gb=12.0, free_gb=104.0)
            ),
            data_dir=tmp_path / "data",
            queue_dir=tmp_path / "queue",
            device_id="device-sim",
            segment_duration_ms=1000,
            video=VideoSettings(),
            upload_limit=unlimited_upload_limit(),
        )
        outcome = run_recording_loop(
            deps, stop, clock=FakeClock(datetime(2026, 11, 18, 9, 0, 0))
        )

    print()
    print(f"--- {name}")
    print(f"    {description}")
    print()
    print(f"    outcome          {outcome.value}")
    print(f"    segments recorded {runner.record_count}")
    print(f"    segments uploaded {len(storage.uploaded)}")
    print(
        "    LED sequence     "
        + " ".join(LED_NAMES.get(call, str(call)) for call in led.calls)
    )
    final = status.upserts[-1]
    print(
        f"    final status     battery_pct={final['battery_pct']} "
        f"recording_active={final['recording_active']} "
        f"segments_pending={final['segments_pending']}"
    )
    if power.power_offs:
        print(
            f"    POWERED OFF      wake-on-charge armed at "
            f"{power.power_offs[0]}%"
        )
    else:
        print("    POWERED OFF      no - the device stayed up")


def main() -> int:
    rule("Part 1: the same traces through the old policy and the new monitor")
    print(
        f"\nThresholds: halt under {BATTERY_HALT_PCT}% (smoothed, confirmed), "
        f"floor under {BATTERY_FLOOR_MV} mV (raw, confirmed),"
    )
    print("warn under 20% and clear at 25%. One sample per recording-loop pass.")
    # The table below is what the monitor decided; the halt logging would only
    # repeat it, and it goes to stderr out of order with this output.
    logging.getLogger("visio_recorder.battery").setLevel(logging.CRITICAL)
    results = {name: replay(name) for name in TRACES}
    logging.getLogger("visio_recorder.battery").setLevel(logging.WARNING)

    rule("Summary")
    print()
    print(f"    {'trace':<30} {'new policy':<22} {'old policy':<22} ")
    wrong = 0
    for name, (new_at, old_at, new_ok) in results.items():
        wrong += 0 if new_ok else 1
        print(f"    {name:<30} {_at(new_at):<22} {_at(old_at):<22}")
    print()
    print(f"    traces the new policy gets wrong: {wrong}")
    old_wrong = sum(
        1
        for name, (_new_at, old_at, _ok) in results.items()
        if (old_at is not None) is not TRACES[name][1]
    )
    print(f"    traces the old policy gets wrong: {old_wrong}")

    rule("Part 2: five traces through the real run_recording_loop")
    for name in (
        "noisy-gauge-healthy-pack",
        "genuine-depletion",
        "sagging-voltage-under-load",
        "halt-then-charger-arrives",
        "depleting-then-plugged-in",
    ):
        run_loop(name)
    print()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="    log: %(message)s")
    raise SystemExit(main())
