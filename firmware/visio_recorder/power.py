"""Ending a low-battery halt in a clean power-off.

The old halt path stopped recording, flashed red and exited 0, which left the
Pi powered and discharging until the rail collapsed - the single most likely
way this device corrupts an SD card. A halt has to end in a shutdown, and the
shutdown has to arm the thing that brings the pendant back, because nobody
should have to reboot a wearable by hand.

Everything here is behind a protocol with a fake, including the PiJuice calls
and ``systemctl poweroff``, so the whole path is exercised in tests without a
device and without any risk of a test turning the machine off.
"""

import logging
from collections.abc import Callable
from typing import Protocol

from visio_recorder.muxer import CommandRunner

POWEROFF_COMMAND = ["systemctl", "poweroff"]

# Seconds the PiJuice waits after being told to cut power. The OS halt is what
# protects the filesystem; this timer is a deadline that fires whether or not
# the halt completed, so a wedged shutdown still ends in a cut rail rather than
# a slow brownout. 60 s is PiJuice's own figure for the same job in
# ``pijuice_sys.py``, chosen to let a halt finish.
PIJUICE_POWER_OFF_DELAY_SEC = 60

_logger = logging.getLogger(__name__)


class PowerController(Protocol):
    """Arms the wake, then powers the device off.

    One method rather than two, because the order is load-bearing and an arm
    without the power-off is meaningless: after the power-off there is no
    process left to arm anything.
    """

    def power_off(self, *, wake_on_charge_pct: int) -> None: ...


class SystemdPowerController:
    """``systemctl poweroff``, for a device with no battery HAT to arm.

    This is a privileged call and the daemon does not run as root, so the
    service user needs a polkit grant for
    ``org.freedesktop.login1.power-off``; the rule ships beside the systemd
    unit. A refusal is logged as an error rather than raised: by this point
    recording has already stopped and the queue has already drained, so there
    is nothing left for an exception to protect.
    """

    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def power_off(self, *, wake_on_charge_pct: int) -> None:
        _logger.warning("battery halt: powering off")
        try:
            self._runner.run(list(POWEROFF_COMMAND))
        except Exception:
            _logger.exception(
                "could not run %s; the device will stay powered and keep "
                "discharging. Check the polkit grant for "
                "org.freedesktop.login1.power-off",
                " ".join(POWEROFF_COMMAND),
            )


class PiJuicePowerApi(Protocol):
    """The three ``pijuice.power`` calls a halt uses.

    Named as the vendor library names them, since this stands in for exactly
    that object. Each returns a dict carrying an ``error`` key rather than
    raising, which is why the controller below inspects the return value.
    """

    def SetWakeUpOnCharge(self, arg: int, non_volatile: bool = False) -> dict: ...

    def SetSystemPowerSwitch(self, state: int) -> dict: ...

    def SetPowerOff(self, delay: int) -> dict: ...


class PiJuicePowerController:
    """The PiJuice's own low-charge shutdown, then the OS halt.

    In order: tell the HAT what charge level to re-power the Pi at, drop the
    5 V rail's load, give the HAT a deadline to cut power, and only then halt
    the OS. Arming first matters because every step after the halt is
    unreachable, and halting last matters because the halt is what protects
    the filesystem.

    No arming failure may stop the power-off. A pendant that could not arm its
    wake still has to stop discharging; losing the automatic wake costs a
    manual power cycle, while skipping the power-off costs the SD card.
    """

    def __init__(
        self,
        power: PiJuicePowerApi,
        runner: CommandRunner,
        power_off_delay_sec: int = PIJUICE_POWER_OFF_DELAY_SEC,
    ) -> None:
        self._power = power
        self._runner = runner
        self._power_off_delay_sec = power_off_delay_sec
        self._systemd = SystemdPowerController(runner)

    def power_off(self, *, wake_on_charge_pct: int) -> None:
        self._try(
            "SetWakeUpOnCharge", self._power.SetWakeUpOnCharge, wake_on_charge_pct
        )
        self._try("SetSystemPowerSwitch", self._power.SetSystemPowerSwitch, 0)
        self._try("SetPowerOff", self._power.SetPowerOff, self._power_off_delay_sec)
        self._systemd.power_off(wake_on_charge_pct=wake_on_charge_pct)

    @staticmethod
    def _try(name: str, call: Callable[[int], dict], argument: int) -> None:
        try:
            result = call(argument)
        except Exception:
            _logger.exception("pijuice %s(%d) raised; continuing", name, argument)
            return
        error = (result or {}).get("error")
        if error not in (None, "NO_ERROR"):
            _logger.error("pijuice %s(%d) returned %s", name, argument, error)


class DisabledPowerController:
    """Logs what it would have done. ``VISIO_BATTERY_POWEROFF=off``.

    For bench work: the whole halt path - the drain, the status push, the red
    LED, the wake level - can be watched on a real device without the device
    going dark before it can be read.
    """

    def power_off(self, *, wake_on_charge_pct: int) -> None:
        _logger.warning(
            "battery halt: power-off suppressed by VISIO_BATTERY_POWEROFF=off "
            "(would have armed wake-on-charge at %d%%)",
            wake_on_charge_pct,
        )
