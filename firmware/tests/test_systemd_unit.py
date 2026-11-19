"""The two unit settings that make the daemon's bounded shutdown real."""

import configparser
from pathlib import Path

UNIT_PATH = Path(__file__).resolve().parents[1] / "systemd" / "visio-recorder.service"


class _SystemdUnit(configparser.ConfigParser):
    """A unit file read as it is written: keys keep their case."""

    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _service_section() -> configparser.SectionProxy:
    unit = _SystemdUnit(strict=False)
    unit.read_string(UNIT_PATH.read_text())
    return unit["Service"]


def test_the_unit_lets_the_daemon_end_its_own_capture():
    # The daemon cuts the in-flight capture short itself on SIGTERM, which
    # only works if systemd signals the daemon alone: the default
    # KillMode=control-group signals rpicam-vid at the same time, and a
    # capture killed from outside loses the finalised short segment the
    # daemon was about to mux and queue.
    assert _service_section()["KillMode"] == "mixed"


def test_the_unit_bounds_how_long_a_stop_may_take():
    # 30 s rather than systemd's 90 s default, which was only ever reached
    # because a stop used to wait out the whole segment. Cancelling the
    # capture, muxing it, queueing it and pushing the final device_status all
    # fit well inside this, so an overrun now means something is wrong rather
    # than that the segment is long.
    assert _service_section()["TimeoutStopSec"] == "30"
