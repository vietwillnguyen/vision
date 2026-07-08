from dataclasses import dataclass
from typing import Protocol

LOW_BATTERY_HALT_PCT = 10
LOW_BATTERY_WARN_PCT = 20


class BatteryReader(Protocol):
    def get_charge_pct(self) -> int: ...


@dataclass
class BatteryStatus:
    pct: int
    should_halt: bool
    is_low: bool


def read_battery_status(reader: BatteryReader) -> BatteryStatus:
    pct = reader.get_charge_pct()
    return BatteryStatus(
        pct=pct,
        should_halt=pct < LOW_BATTERY_HALT_PCT,
        is_low=pct < LOW_BATTERY_WARN_PCT,
    )
