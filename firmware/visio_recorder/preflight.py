"""Runtime environment verification for visio-recorder.

Checks are grouped by severity: BLOCK means the daemon cannot function
without it - the check's failure should stop the daemon from starting.
WARN means the daemon is unaffected: the PiJuice/I2C checks, since a
missing battery HAT is an accepted, expected state while
VISIO_BATTERY_SOURCE=none (see
docs/superpowers/specs/2026-07-21-visio-device-provisioning-design.md);
the microphone check, since a pendant without one records video only; the
fixed-core-clock check, since a scaling clock skews LED colours rather than
stopping anything; and the data-dir and SPI-device checks when the path
exists but cannot be used by a caller other than the uid the daemon runs as.

Severity is therefore not always a property of the check alone - it can
depend on who is running it, because this module is both the daemon's
own startup gate and a diagnostic an operator runs over SSH. Only the
genuinely caller-relative part is downgraded: a data dir that does not
exist at all still blocks for every caller.

Every check takes an injectable seam so it can be unit tested without
touching real hardware, matching the rest of firmware/'s Protocol/fake
convention.
"""

import importlib.util
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from visio_recorder.capture import AudioSettings, probe_audio_device

BLOCK = "block"
WARN = "warn"

# The one definition of where recordings go. ``daemon.load_config`` resolves
# its own data dir through ``resolve_data_dir`` below rather than carrying a
# second default, so the gate and the daemon cannot drift apart.
_DEFAULT_DATA_DIR = "/var/lib/visio-recorder"
_DATA_DIR = Path(_DEFAULT_DATA_DIR)
_I2C_DEVICE = Path("/dev/i2c-1")
# The status LED's bus: SPI0, chip select 0 (pendant spec, "LED driver").
_SPI_DEVICE = Path("/dev/spidev0.0")
_CONFIG_TXT = "/boot/firmware/config.txt"
_ASOUND_CARDS = Path("/proc/asound/cards")
_ASOUND_CONF = Path("/etc/asound.conf")
# The I2S card dtoverlay=adau7002-simple registers.
_I2S_CARD = "adau7002"

# The systemd unit's EnvironmentFile=. Written mode 600 root-owned by
# setup-device.sh, which is why reading it is a best effort here.
_ENV_FILE = Path("/etc/visio-recorder.env")
_ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

# The uid the systemd unit starts the daemon as (User= is unset, so root).
# Only that caller is gated on the data dir being writable.
_DAEMON_UID = 0


@dataclass
class CheckResult:
    name: str
    severity: str
    ok: bool
    detail: str


def check_binary_on_path(
    name: str, which: Callable[[str], str | None] = shutil.which
) -> CheckResult:
    found = which(name) is not None
    return CheckResult(
        name=name,
        severity=BLOCK,
        ok=found,
        detail="found on PATH"
        if found
        else f"{name} not found on PATH - check the apt install step",
    )


# Where each blocking module actually comes from on a provisioned device, so
# the report points at the step that failed rather than at whichever one is
# most familiar. Only supabase is a uv.lock entry: gpiozero and spidev are apt
# packages reached through the venv's --system-site-packages.
_IMPORT_REMEDY = {
    "spidev": (
        "installed by apt as python3-spidev and visible here only because the venv "
        "is built with --system-site-packages - check the apt install step"
    ),
    "gpiozero": (
        "installed by apt as python3-gpiozero and visible here only because the venv "
        "is built with --system-site-packages - check the apt install step"
    ),
    "supabase": "check the uv sync step",
}


def check_importable(
    module: str,
    find_spec: Callable[[str], object | None] = importlib.util.find_spec,
    remedy: str | None = None,
) -> CheckResult:
    found = find_spec(module) is not None
    if remedy is None:
        remedy = _IMPORT_REMEDY.get(module, "check the uv sync step")
    return CheckResult(
        name=module,
        severity=BLOCK,
        ok=found,
        detail="importable" if found else f"{module} not importable - {remedy}",
    )


def check_pijuice_importable(
    find_spec: Callable[[str], object | None] = importlib.util.find_spec,
) -> CheckResult:
    found = find_spec("pijuice") is not None
    return CheckResult(
        name="pijuice",
        severity=WARN,
        ok=found,
        detail="pijuice importable"
        if found
        else "pijuice not installed - expected when VISIO_BATTERY_SOURCE=none",
    )


def check_camera_detected(
    run: Callable[[list[str]], subprocess.CompletedProcess] = lambda args: (
        subprocess.run(args, capture_output=True, text=True, timeout=5)
    ),
) -> CheckResult:
    try:
        result = run(["rpicam-hello", "--list-cameras"])
    except (FileNotFoundError, PermissionError) as exc:
        return CheckResult(
            name="camera",
            severity=BLOCK,
            ok=False,
            detail=(
                f"rpicam-hello unavailable - {type(exc).__name__}: "
                "check apt install step"
            ),
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="camera",
            severity=BLOCK,
            ok=False,
            detail="rpicam-hello timeout - camera may be unresponsive",
        )
    detected = (
        result.returncode == 0
        and re.search(r"^\d+\s*:", result.stdout, re.MULTILINE) is not None
    )
    return CheckResult(
        name="camera",
        severity=BLOCK,
        ok=detected,
        detail="camera detected"
        if detected
        else "no camera detected - check ribbon cable connection",
    )


def check_networkmanager_running(
    run: Callable[[list[str]], subprocess.CompletedProcess] = lambda args: (
        subprocess.run(args, capture_output=True, text=True, timeout=5)
    ),
) -> CheckResult:
    try:
        result = run(["nmcli", "general", "status"])
    except (FileNotFoundError, PermissionError) as exc:
        return CheckResult(
            name="networkmanager",
            severity=BLOCK,
            ok=False,
            detail=f"nmcli unavailable - {type(exc).__name__}: check apt install step",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="networkmanager",
            severity=BLOCK,
            ok=False,
            detail="nmcli timeout - NetworkManager may be unresponsive",
        )
    ok = result.returncode == 0
    return CheckResult(
        name="networkmanager",
        severity=BLOCK,
        ok=ok,
        detail="running"
        if ok
        else "nmcli general status failed - is NetworkManager active?",
    )


def check_data_dir_writable(
    path: Path = _DATA_DIR,
    getuid: Callable[[], int] = os.getuid,
    can_write: Callable[[Path], bool] = lambda path: os.access(path, os.W_OK),
    caveat: str = "",
) -> CheckResult:
    """Existence and writability answer different questions, so they differ.

    Whether the directory exists is caller-independent - /var/lib is 0755
    root:root, so a missing data dir looks missing to every uid - and a device
    without one is not provisioned: setup-device.sh exits at the env-file step
    before it ever reaches the data-dir step, both on a fresh device and
    whenever the Supabase keys are unfilled. That stays BLOCK for everyone.

    Writability is caller-relative, and only it. The daemon runs as
    ``_DAEMON_UID`` and cannot function without a writable data dir, so for
    that caller an existing-but-unwritable directory stays BLOCK - the same
    gate the unit's ExecStartPre has always applied. For an operator running
    the diagnostic over SSH it is *expected*: the directory stays root-owned
    on purpose because it holds recorded video, so reporting BLOCK made a
    healthy device look mis-provisioned and advised a re-run of
    setup-device.sh that would change nothing.

    ``caveat`` is appended to whichever detail is reported, for the caller that
    could not confirm which directory is configured (see
    ``resolve_reported_data_dir``). It never changes a severity: which
    directory was checked and how certain that choice is are separate facts
    from whether the directory that *was* checked is usable.
    """
    suffix = f" [{caveat}]" if caveat else ""
    exists = path.is_dir()
    if exists and can_write(path):
        return CheckResult(
            name="data_dir", severity=BLOCK, ok=True, detail=f"{path} writable{suffix}"
        )
    uid = getuid()
    if not exists or uid == _DAEMON_UID:
        return CheckResult(
            name="data_dir",
            severity=BLOCK,
            ok=False,
            detail=f"{path} missing or not writable - run setup-device.sh{suffix}",
        )
    return CheckResult(
        name="data_dir",
        severity=WARN,
        ok=False,
        detail=(
            f"{path} exists but is not writable by uid {uid} - expected for a "
            f"diagnostic run as a non-daemon user, not a fault; the daemon runs "
            f"as uid {_DAEMON_UID}{suffix}"
        ),
    )


def _open_read_write(path: Path) -> None:
    os.close(os.open(path, os.O_RDWR))


def check_spi_device(
    path: Path = _SPI_DEVICE,
    getuid: Callable[[], int] = os.getuid,
    open_device: Callable[[Path], None] = _open_read_write,
) -> CheckResult:
    """The LED driver's SPI node exists and opens read-write.

    Opened rather than stat'ed, because opening is what the driver does and
    what a missing ``spi`` group membership refuses. A missing node means SPI0
    is off for everyone; a refusal is caller-relative exactly as the data
    dir's writability is (see ``check_data_dir_writable``), so only the
    daemon's own uid is blocked by it.
    """
    try:
        open_device(path)
    except FileNotFoundError:
        return CheckResult(
            name="spidev0.0",
            severity=BLOCK,
            ok=False,
            detail=(
                f"{path} missing - SPI0 is off: setup-device.sh sets dtparam=spi=on in "
                f"{_CONFIG_TXT}, then reboot so it takes effect"
            ),
        )
    except PermissionError:
        uid = getuid()
        if uid == _DAEMON_UID:
            return CheckResult(
                name="spidev0.0",
                severity=BLOCK,
                ok=False,
                detail=f"{path} exists but uid {uid} cannot open it read-write",
            )
        return CheckResult(
            name="spidev0.0",
            severity=WARN,
            ok=False,
            detail=(
                f"{path} exists but uid {uid} cannot open it - add this user to the "
                f"spi group to drive the LED by hand; the daemon runs as uid "
                f"{_DAEMON_UID}, so this is not a fault"
            ),
        )
    except OSError as exc:
        return CheckResult(
            name="spidev0.0",
            severity=BLOCK,
            ok=False,
            detail=f"{path} did not open - {type(exc).__name__}: {exc}",
        )
    return CheckResult(
        name="spidev0.0", severity=BLOCK, ok=True, detail=f"{path} opens read-write"
    )


def check_core_freq_fixed(
    run: Callable[[list[str]], subprocess.CompletedProcess] = lambda args: (
        subprocess.run(args, capture_output=True, text=True, timeout=5)
    ),
) -> CheckResult:
    """Whether the firmware holds the core clock fixed, as the LED timing needs.

    spi-bcm2835 caches the core clock when it probes, so with the Zero 2W's
    core clock scaling between 250 and 400 MHz the SPI rate - and with it the
    WS2812B bit timing - can drift by up to 1.6x. Wrong colours, not a stopped
    daemon, so WARN.
    """
    remedy = f"set core_freq_fixed=1 in {_CONFIG_TXT} (setup-device.sh does) and reboot"
    try:
        result = run(["vcgencmd", "get_config", "core_freq_fixed"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            name="core_freq_fixed",
            severity=WARN,
            ok=False,
            detail=f"could not ask vcgencmd ({type(exc).__name__}) - {remedy}",
        )
    ok = result.returncode == 0 and result.stdout.strip() == "core_freq_fixed=1"
    return CheckResult(
        name="core_freq_fixed",
        severity=WARN,
        ok=ok,
        detail="core clock held fixed"
        if ok
        else (
            f"firmware reports {result.stdout.strip() or 'nothing'} - LED colours can "
            f"drift with the core clock: {remedy}"
        ),
    )


class _QuietRunner:
    """Runs the probe with its stderr captured, so ffmpeg's and ALSA's own
    error lines do not interleave with the report (or flood journalctl from
    ExecStartPre); the check's detail says what failed instead."""

    def run(self, args: list[str]) -> None:
        subprocess.run(args, check=True, capture_output=True, timeout=10)


def _probe_with_ffmpeg(device: str) -> bool:
    try:
        return probe_audio_device(_QuietRunner(), AudioSettings(device=device))
    except subprocess.SubprocessError:
        return False  # a timeout: the device did not deliver samples


def _read_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def check_audio_device(
    device: str | None,
    probe: Callable[[str], bool] | None = None,
    cards_path: Path = _ASOUND_CARDS,
    asound_conf: Path = _ASOUND_CONF,
) -> CheckResult:
    """Whether the microphone the daemon will record from opens - WARN only.

    The open itself is the daemon's own startup probe
    (``capture.probe_audio_device``), so the two cannot disagree. When it
    fails, the layers under it are checked from the bottom up so the report
    names the one that is missing: the I2S card the overlay registers, then
    the ALSA PCM ``/etc/asound.conf`` defines, then the wiring.
    """
    if device is None:
        return CheckResult(
            name="audio",
            severity=WARN,
            ok=True,
            detail="VISIO_AUDIO=off - recording video only, microphone not checked",
        )
    if (_probe_with_ffmpeg if probe is None else probe)(device):
        return CheckResult(
            name="audio", severity=WARN, ok=True, detail=f"ALSA device {device} opens"
        )
    fallback = "the daemon will record video only"
    if device == AudioSettings().device and _I2S_CARD not in _read_or_empty(cards_path):
        cause = (
            f"no {_I2S_CARD} sound card in {cards_path} - dtoverlay=adau7002-simple is "
            f"missing from {_CONFIG_TXT} (setup-device.sh adds it) or the device has "
            f"not been rebooted since"
        )
    elif (
        re.search(
            rf"^\s*pcm\.{re.escape(device)}\b",
            _read_or_empty(asound_conf),
            re.MULTILINE,
        )
        is None
        and ":" not in device
    ):
        cause = (
            f"{device} is not defined in {asound_conf} - install "
            f"firmware/alsa/visio-mic.conf there (setup-device.sh step 6 does)"
        )
    else:
        cause = (
            f"{device} is defined and the card is present but it did not open - "
            f"check the INMP441 wiring (SCK GPIO 18, WS GPIO 19, SD GPIO 20, L/R GND), "
            f"or whether it is in use by a running visio-recorder"
        )
    return CheckResult(
        name="audio", severity=WARN, ok=False, detail=f"{cause}; {fallback}"
    )


def check_i2c_enabled(path: Path = _I2C_DEVICE) -> CheckResult:
    ok = path.exists()
    return CheckResult(
        name="i2c",
        severity=WARN,
        ok=ok,
        detail="I2C bus enabled"
        if ok
        else "I2C not enabled - run setup-device.sh, may need a reboot",
    )


def resolve_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """The directory the daemon records into, given an environment.

    The single definition of that mapping: ``daemon.load_config`` calls this
    too, so there is no second key, default or precedence to keep in step.
    """
    return Path(
        (os.environ if env is None else env).get("VISIO_DATA_DIR", _DEFAULT_DATA_DIR)
    )


def read_env_file(path: Path = _ENV_FILE) -> Mapping[str, str] | None:
    """Parse the unit's ``EnvironmentFile``, or ``None`` if it cannot be read.

    The two outcomes are deliberately distinct. An absent file means nothing
    is configured, so the defaults are the truth. A file that exists but
    cannot be read means the truth is unknown - the normal case for an
    operator over SSH, since setup-device.sh writes it mode 600 root-owned
    and that is not relaxed for a diagnostic's benefit.

    Parsing matches how systemd reads an EnvironmentFile, and how
    setup-device.sh's own ``env_file_value`` reads it: comments skipped, last
    assignment wins, leading whitespace before a key tolerated, and
    surrounding whitespace plus one layer of matching quotes stripped from
    the value.

    The encoding is pinned rather than taken from the locale, which under
    systemd is whatever the unit inherited and is routinely C/POSIX; decoding
    a UTF-8 path as ASCII would otherwise fail on a perfectly valid file. A
    decode failure that survives that is still a can't-read case, and returns
    ``None`` like any other - this function is a diagnostic's input, so
    raising here would replace the whole report with a traceback in
    journalctl.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ENV_LINE.match(line)
        if match is None:
            continue
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[match.group(1)] = value
    return values


@dataclass
class DataDirResolution:
    path: Path
    caveat: str = ""


def resolve_reported_data_dir(
    env: Mapping[str, str] | None = None,
    env_file: Path = _ENV_FILE,
    read_file: Callable[[Path], Mapping[str, str] | None] | None = None,
) -> DataDirResolution:
    """Which data dir to check, and how sure we are that it is the right one.

    ``ExecStartPre`` inherits ``VISIO_DATA_DIR`` from the unit's
    ``EnvironmentFile=``, so an environment variable always wins. The
    standalone ``visio-preflight`` gets no such environment - nothing sources
    the env file into an operator's shell - so fall back to reading it.

    When it exists but cannot be read, the default is still the only path
    available to check, but reporting it as though it were the configured one
    would be a guess dressed as a fact: a device with ``VISIO_DATA_DIR``
    pointed elsewhere would show a blocking failure for a directory that is
    correctly absent. The caveat says so instead.
    """
    environ = os.environ if env is None else env
    if "VISIO_DATA_DIR" in environ:
        return DataDirResolution(Path(environ["VISIO_DATA_DIR"]))
    configured = (read_env_file if read_file is None else read_file)(env_file)
    if configured is None:
        return DataDirResolution(
            Path(_DEFAULT_DATA_DIR),
            caveat=(
                f"checked the default: {env_file} could not be read or parsed, so a "
                f"configured VISIO_DATA_DIR could not be confirmed - it is mode 600 "
                f"root-owned, so re-run under sudo to check the configured path; if "
                f"that fails too the file itself is unreadable or malformed"
            ),
        )
    return DataDirResolution(resolve_data_dir(configured))


def resolve_battery_source(
    env: Mapping[str, str] | None = None,
    env_file: Path = _ENV_FILE,
    read_file: Callable[[Path], Mapping[str, str] | None] | None = None,
) -> str:
    """Same resolution order as the data dir, for the same reason.

    Only the pijuice and I2C checks depend on this, and both are WARN, so an
    unreadable env file costs at most two spurious warnings rather than a
    wrong answer - no caveat is threaded through for it.
    """
    environ = os.environ if env is None else env
    if "VISIO_BATTERY_SOURCE" in environ:
        return environ["VISIO_BATTERY_SOURCE"]
    configured = (read_env_file if read_file is None else read_file)(env_file)
    return (configured or {}).get("VISIO_BATTERY_SOURCE", "pijuice")


def resolve_audio_device(
    env: Mapping[str, str] | None = None,
    env_file: Path = _ENV_FILE,
    read_file: Callable[[Path], Mapping[str, str] | None] | None = None,
) -> str | None:
    """The ALSA device the daemon will try, or None when ``VISIO_AUDIO=off``.

    Same keys and defaults as ``daemon.load_config``, resolved per key the way
    the battery source is: the environment first, then the env file.
    """
    environ = os.environ if env is None else env
    configured: Mapping[str, str] | None = None

    def value(key: str, default: str) -> str:
        nonlocal configured
        if key in environ:
            return environ[key]
        if configured is None:
            configured = (read_env_file if read_file is None else read_file)(
                env_file
            ) or {}
        return configured.get(key, default)

    if value("VISIO_AUDIO", "on").strip().lower() == "off":
        return None
    return value("VISIO_AUDIO_DEVICE", AudioSettings().device)


def run_checks(
    battery_source: str,
    data_dir: Path = _DATA_DIR,
    data_dir_caveat: str = "",
    audio_device: str | None = AudioSettings().device,
) -> list[CheckResult]:
    results = [
        check_binary_on_path("rpicam-vid"),
        check_binary_on_path("rpicam-still"),
        check_binary_on_path("zbarimg"),
        check_binary_on_path("ffmpeg"),
        check_binary_on_path("nmcli"),
        check_importable("spidev"),
        check_importable("gpiozero"),
        check_importable("supabase"),
        check_spi_device(),
        check_core_freq_fixed(),
        check_camera_detected(),
        check_networkmanager_running(),
        check_data_dir_writable(path=data_dir, caveat=data_dir_caveat),
        check_audio_device(audio_device),
    ]
    if battery_source == "pijuice":
        results.append(check_pijuice_importable())
        results.append(check_i2c_enabled())
    return results


def has_blocking_failure(results: list[CheckResult]) -> bool:
    return any(not r.ok and r.severity == BLOCK for r in results)


def install_root(module_file: str = __file__) -> Path:
    """Directory the running ``visio_recorder`` package was imported from.

    A device carries two clones - the provisioned one under
    /opt/visio-recorder and the operator's own working clone - and nothing at
    the shell prompt distinguishes them. Reporting the resolved path makes
    every preflight transcript self-identifying, whether it came from an SSH
    session or from the unit's ExecStartPre in ``journalctl``.
    """
    return Path(module_file).resolve().parent


def format_report(results: list[CheckResult], root: Path | None = None) -> str:
    lines = [f"[INFO] install: {root if root is not None else install_root()}"]
    lines += [
        f"[{'OK' if r.ok else r.severity.upper()}] {r.name}: {r.detail}"
        for r in results
    ]
    return "\n".join(lines)


def main() -> int:
    """CLI entrypoint, exposed as the ``visio-preflight`` console script.

    Also reachable as ``python3 -m visio_recorder.preflight``; both resolve
    from any working directory once the package is actually installed into
    the venv (see this package's pyproject.toml [build-system] note).

    Thin on purpose, same convention as daemon.py's main() - no branching
    logic beyond what run_checks/has_blocking_failure already own, so it's
    not unit tested itself.
    """
    data_dir = resolve_reported_data_dir()
    results = run_checks(
        resolve_battery_source(),
        data_dir.path,
        data_dir.caveat,
        audio_device=resolve_audio_device(),
    )
    print(format_report(results))
    return 1 if has_blocking_failure(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
