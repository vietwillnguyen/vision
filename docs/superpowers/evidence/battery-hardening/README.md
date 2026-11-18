# Battery hardening - simulation evidence

Everything here is simulation.
No Pi, no PiJuice, no real Supabase project, and no test or script that can reach a real `systemctl poweroff`: the power-off and every PiJuice call go through a protocol with a fake.
The design this validates is the pendant spec's "Battery and Power Management" section.

## What the firmware used to do

[`00-before-battery-policy.txt`](00-before-battery-policy.txt) is the whole of `battery.py` at the base commit (`5073cce`), twenty-five lines.
One reading, two comparisons:

```python
should_halt=pct < LOW_BATTERY_HALT_PCT,
is_low=pct < LOW_BATTERY_WARN_PCT,
```

No average, no confirmations, no voltage, and no idea whether a charger is connected.
A halt reached from it stopped recording, flashed red and exited 0, leaving the Pi powered and discharging.

[`unit-before.txt`](unit-before.txt) is the new test suite run against that commit's firmware, extracted with `git archive` into a temp directory: three collection errors, because none of what the tests exercise exists yet.

## The traces, old policy beside new

[`e2e_battery_traces.py`](e2e_battery_traces.py) is the harness; [`e2e-traces.txt`](e2e-traces.txt) is its output.
Run it from `firmware/`:

```sh
uv run --locked --extra dev python \
    ../docs/superpowers/evidence/battery-hardening/e2e_battery_traces.py
```

**Part 1** replays eight recorded-shape traces through two deciders over the same samples: `old_policy`, which is the base commit's halt rule verbatim, and the real `BatteryMonitor`.
Each trace carries the outcome a correct firmware must produce, so this is a check rather than a printout.
One sample per recording-loop pass, which is one per segment - a minute apart at the firmware's default segment length.

| Trace | Must halt | Old policy | New policy |
|---|---|---|---|
| `noisy-gauge-healthy-pack` | no | halts at sample 2 | never halts |
| `genuine-depletion` | yes | halts at sample 7 | halts at sample 11 |
| `sagging-voltage-under-load` | yes | **never halts** | halts at sample 4 |
| `transmit-burst-sag` | no | never halts | never halts |
| `flat-but-charging` | no | halts at sample 1 | never halts |
| `hovering-at-the-warn-line` | no | never halts | never halts |
| `halt-then-charger-arrives` | yes | never halts | halts at sample 3 |
| `depleting-then-plugged-in` | no | halts at sample 3 | never halts |

The old policy is wrong on five of the eight; the new one is wrong on none.

The four it gets wrong by halting are the point of the debounce and the charging exemption: a gauge spiking down under load, a pendant plugged in at 4 %, and one that was falling until a charger arrived.
The one it gets wrong by *not* halting is the point of the voltage floor - the gauge still reads 53-55 % while the cell's terminal voltage has already collapsed, because the PiJuice's gauge is resistance-compensated against a battery profile and lags under exactly the camera-plus-WiFi load the pendant runs at.

### The cost, stated rather than hidden

`genuine-depletion` is the trace where the new policy is slower: sample 11 against the old policy's sample 7.
That lag is structural and roughly `window / 2 + confirmations` samples - the moving average trails a falling trend by about half its window, and three confirmations follow.

Its size in charge depends entirely on how fast the pack is actually falling.
That trace drops 2 % per sample, which is about ten times faster than the real thing: a 3000 mAh pack at the spec's 1.5 W budget loses roughly 0.2 % a minute, so four samples of lag is under 1 % of charge.
The trace is deliberately steep so the lag is visible at all.

The lag is also what the voltage floor covers.
On any pack falling fast enough for four samples to matter, the terminal voltage reaches 3300 mV first, and the floor confirms in two readings rather than three against the raw value rather than the average.

## The traces through the real recording loop

**Part 2** of the same output runs five of those traces through the real `run_recording_loop`, with the camera and ffmpeg faked by the firmware test suite's own `FakeCommandRunner` and the power-off behind `FakePowerController`.

| Trace | Outcome | Final LED | Powered off |
|---|---|---|---|
| `noisy-gauge-healthy-pack` | `stopped` | green | no |
| `genuine-depletion` | `halted` | RED | yes, wake armed at 25 % |
| `sagging-voltage-under-load` | `halted` | RED | yes, wake armed at 25 % |
| `halt-then-charger-arrives` | `recovered` | green | **no** |
| `depleting-then-plugged-in` | `stopped` | green | no |

Every halt drains its queue first (`segments_pending=0` in the final status row), pushes `recording_active=False`, and shows the Critical red last, after the worker join, so nothing can paint over it.

`halt-then-charger-arrives` is the recovery path end to end.
Its LED sequence is `blue green RED green`: the halt is declared and the red shown, then the re-read finds external power, the power-off is cancelled, and the red gives way to the live state rather than leaving a resuming pendant sitting red.
The loop returns `recovered`, which is what makes the daemon re-enter it instead of exiting.

## Unit tests

[`unit-after.txt`](unit-after.txt): the whole firmware suite, 307 passing.
The debounce itself (`test_battery.py`), the power-off sequencing and both of the vendor library's failure shapes (`test_power.py`), the HAT's status-word translation (`test_drivers.py`), and the loop-level halt, bounded drain, recovery and charging paths (`test_daemon.py`).

Two of those deserve naming because they are the regressions this work exists to prevent:

- `test_a_fixed_full_percentage_only_source_never_halts_or_warns` drives the whole loop with a reader that reports a fixed 100 % and implements no telemetry - the `VISIO_BATTERY_SOURCE=none` shape - and asserts no latch, no halt, no power-off.
- `test_a_wedged_upload_cannot_hold_a_dying_battery_open_forever` wedges an upload that never returns and asserts the halt still powers off, which is the bound on the drain doing its job.

## systemd and polkit

[`systemd-analyze-verify.txt`](systemd-analyze-verify.txt): `systemd-analyze verify` on the service unit, and a JavaScript syntax check of the polkit rule (polkit rules are JavaScript; the file is copied to a `.js` name because `node --check` goes by extension).
Both exit 0.

The unit itself is unchanged by this work.
`KillMode=mixed` and `TimeoutStopSec` belong to the recording stop/start control design, which owns the interruptible capture that makes them meaningful.

## What only the bench can confirm

Five steps, added to Task 9 of the device-provisioning plan and blocked until a PiJuice pHAT or a replacement fuel gauge is sourced:

- the HAT's real telemetry shapes against this firmware's reader;
- the voltage sag this pack shows under this load, which is the only way to know 3300 mV is the right floor for it;
- the halt, the drain, the power-off and the wake, with `SetWakeUpOnCharge` acting on a register after the Pi is already off - the half that cannot be faked at all;
- that `systemctl poweroff` is actually authorized for the service user, since a polkit refusal is silent in production;
- that a charging pendant never halts, and that disconnecting the charger lets the halt proceed.
