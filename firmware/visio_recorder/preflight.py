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

The daemon runs as the unprivileged ``visio`` user, so whether *that* user
can open each device node is checked separately from whether the caller
can: the ``*_access`` checks always answer for the daemon's user, from its
real process credentials when this is the unit's own ExecStartPre, and from
its account plus the unit's ``SupplementaryGroups=`` otherwise - so an
operator over SSH, with or without sudo, sees what the daemon will see.

Severity is therefore not always a property of the check alone - it can
depend on who is running it, because this module is both the daemon's
own startup gate and a diagnostic an operator runs over SSH. Only the
genuinely caller-relative part is downgraded: a data dir that does not
exist at all still blocks for every caller.

Every check takes an injectable seam so it can be unit tested without
touching real hardware, matching the rest of firmware/'s Protocol/fake
convention.
"""

import glob as globmod
import grp
import importlib.util
import os
import pwd
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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

# The systemd unit's EnvironmentFile=. Written mode 0640 root:visio by
# setup-device.sh, which is why reading it is a best effort here.
_ENV_FILE = Path("/etc/visio-recorder.env")
_ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

# Who the systemd unit runs the daemon as. Both are pinned to
# visio-recorder.service by tests; at runtime the installed unit's own values
# win (``resolve_daemon_identity``), so a rollback drop-in setting User=root is
# reported as what it is rather than as a broken visio account.
_UNIT_NAME = "visio-recorder.service"
_DAEMON_USER = "visio"
_DAEMON_GROUPS: tuple[str, ...] = ("video", "gpio", "i2c", "spi", "audio")


@dataclass
class CheckResult:
    name: str
    severity: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DaemonIdentity:
    """The account the unit runs the daemon as, and the groups it gets.

    ``uid`` is None when the account does not exist. ``groups`` maps gid to
    name for the account's primary group, every group listing it as a member,
    and the unit's ``SupplementaryGroups=`` - which systemd grants even when
    the account's own membership has drifted.
    """

    user: str
    uid: int | None
    groups: Mapping[int, str]
    source: str


def _systemctl_show() -> Mapping[str, str] | None:
    try:
        result = subprocess.run(
            ["systemctl", "show", _UNIT_NAME,
             "--property=LoadState,User,SupplementaryGroups"],
            capture_output=True, text=True, timeout=5,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )


class _Account(Protocol):
    @property
    def pw_uid(self) -> int: ...
    @property
    def pw_gid(self) -> int: ...


class _Group(Protocol):
    @property
    def gr_name(self) -> str: ...
    @property
    def gr_gid(self) -> int: ...
    @property
    def gr_mem(self) -> list[str]: ...


def resolve_daemon_identity(
    show: Callable[[], Mapping[str, str] | None] = _systemctl_show,
    getpwnam: Callable[[str], _Account] = pwd.getpwnam,
    getgrall: Callable[[], Sequence[_Group]] = grp.getgrall,
) -> DaemonIdentity:
    """Who the daemon runs as: the installed unit's word, else the packaged one.

    The loaded unit (with any drop-ins) is authoritative, because that is what
    systemd will actually do; an empty ``User=`` there means root. When the
    unit is not loaded - CI, or a device not yet provisioned - the packaged
    defaults above are the best answer.
    """
    props = show()
    user, supplementary = _DAEMON_USER, _DAEMON_GROUPS
    source = "packaged unit defaults"
    if props is not None and props.get("LoadState") == "loaded":
        user = props.get("User") or "root"
        supplementary = tuple(props.get("SupplementaryGroups", "").split())
        source = f"installed {_UNIT_NAME}"
    try:
        account = getpwnam(user)
    except KeyError:
        return DaemonIdentity(user=user, uid=None, groups={}, source=source)
    all_groups = getgrall()
    groups = {
        g.gr_gid: g.gr_name
        for g in all_groups
        if g.gr_gid == account.pw_gid or user in g.gr_mem or g.gr_name in supplementary
    }
    return DaemonIdentity(user=user, uid=account.pw_uid, groups=groups, source=source)


def _group_names(groups: Mapping[int, str]) -> str:
    return " ".join(sorted(groups.values())) or "none"


def check_daemon_user(daemon: DaemonIdentity) -> CheckResult:
    if daemon.uid is None:
        return CheckResult(
            name="daemon_user",
            severity=BLOCK,
            ok=False,
            detail=(
                f"the account {daemon.user!r} ({daemon.source}) does not exist, so "
                f"systemd cannot start the daemon - run setup-device.sh, which "
                f"creates it"
            ),
        )
    return CheckResult(
        name="daemon_user",
        severity=BLOCK,
        ok=True,
        detail=(
            f"{daemon.user} (uid {daemon.uid}, {daemon.source}), groups: "
            f"{_group_names(daemon.groups)}"
        ),
    )


@dataclass(frozen=True)
class DeviceRequirement:
    """Device nodes a part of the daemon opens, and the group that grants them."""

    name: str
    patterns: tuple[str, ...]
    group: str
    severity: str
    needed_for: str


# Raspberry Pi OS's udev rules hand each class to a group with mode 0660:
# systemd's 50-udev-default.rules gives video4linux and media nodes to video
# and sound nodes to audio; raspberrypi-sys-mods' 99-com.rules gives spidev to
# spi, gpiochip and gpiomem to gpio, i2c-dev to i2c and dma_heap to video.
# libcamera opens the media controller, the video and subdev nodes and the DMA
# heaps, so all of them are the camera's.
CAMERA_NODES = DeviceRequirement(
    name="camera_access",
    patterns=("/dev/media*", "/dev/video*", "/dev/v4l-subdev*", "/dev/dma_heap/*"),
    group="video",
    severity=BLOCK,
    needed_for="rpicam-vid and rpicam-still (libcamera)",
)
LED_NODES = DeviceRequirement(
    name="spi_access",
    patterns=(str(_SPI_DEVICE),),
    group="spi",
    severity=BLOCK,
    needed_for="the status LED",
)
BUTTON_NODES = DeviceRequirement(
    name="gpio_access",
    patterns=("/dev/gpiochip*",),
    group="gpio",
    severity=BLOCK,
    needed_for="the flag button (gpiozero)",
)
MIC_NODES = DeviceRequirement(
    name="audio_access",
    patterns=("/dev/snd/*",),
    group="audio",
    severity=WARN,
    needed_for="the I2S microphone (video-only recording without it)",
)
I2C_NODES = DeviceRequirement(
    name="i2c_access",
    patterns=(str(_I2C_DEVICE),),
    group="i2c",
    severity=WARN,
    needed_for="the PiJuice battery HAT",
)


def _grants_read_write(st: os.stat_result, uid: int, gids: set[int]) -> bool:
    """The kernel's DAC decision for O_RDWR, for an account that is not us."""
    if uid == 0:
        return True
    if st.st_uid == uid:
        bits = st.st_mode >> 6
    elif st.st_gid in gids:
        bits = st.st_mode >> 3
    else:
        bits = st.st_mode
    return bits & 0o6 == 0o6


def _group_name(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def _owner_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def check_device_access(
    requirement: DeviceRequirement,
    daemon: DaemonIdentity,
    getuid: Callable[[], int] = os.getuid,
    getgroups: Callable[[], list[int]] = lambda: [os.getgid(), *os.getgroups()],
    glob: Callable[[str], list[str]] = globmod.glob,
    stat_node: Callable[[str], os.stat_result] = os.stat,
    access: Callable[[str], bool] = lambda path: os.access(path, os.R_OK | os.W_OK),
    group_name: Callable[[int], str] = _group_name,
    owner_name: Callable[[int], str] = _owner_name,
) -> CheckResult:
    """Whether the daemon's user can open every node ``requirement`` names.

    Answered for the daemon's user whoever runs it. As the daemon itself
    (the unit's ExecStartPre) the kernel is asked directly, with the process's
    real groups; for anyone else - an operator, or root under sudo, who could
    open the nodes themselves - the daemon's account and the unit's
    ``SupplementaryGroups=`` are checked against each node's owner, group and
    mode. Either way a refusal names the node, how it is owned, and the group
    that is missing.
    """
    nodes: list[tuple[str, os.stat_result]] = []
    for pattern in requirement.patterns:
        for path in sorted(glob(pattern)):
            try:
                st = stat_node(path)
            except OSError:
                continue
            if not stat.S_ISDIR(st.st_mode):
                nodes.append((path, st))
    wanted = ", ".join(requirement.patterns)
    if not nodes:
        return CheckResult(
            name=requirement.name,
            severity=requirement.severity,
            ok=False,
            detail=(
                f"no {wanted} on this system, so {requirement.needed_for} has "
                f"nothing to open - see the matching check above"
            ),
        )
    if daemon.uid is None:
        return CheckResult(
            name=requirement.name,
            severity=requirement.severity,
            ok=False,
            detail=f"cannot check: the daemon account {daemon.user!r} does not exist",
        )
    is_daemon = getuid() == daemon.uid
    if is_daemon:
        groups = {gid: group_name(gid) for gid in getgroups()}
        how = "this process"
    else:
        groups = dict(daemon.groups)
        how = f"{daemon.user}'s account and the unit's SupplementaryGroups="
    denied = [
        (path, st)
        for path, st in nodes
        if not (
            access(path)
            if is_daemon
            else _grants_read_write(st, daemon.uid, set(groups))
        )
    ]
    if not denied:
        return CheckResult(
            name=requirement.name,
            severity=requirement.severity,
            ok=True,
            detail=(
                f"{daemon.user} can open {len(nodes)} node(s) for "
                f"{requirement.needed_for} ({wanted})"
            ),
        )
    described = "; ".join(
        f"{path} ({owner_name(st.st_uid)}:{group_name(st.st_gid)} "
        f"{stat.S_IMODE(st.st_mode):04o})"
        for path, st in denied
    )
    node_groups = {group_name(st.st_gid) for _, st in denied}
    if requirement.group not in groups.values():
        cause = (
            f"{daemon.user} is not in the {requirement.group} group (groups per "
            f"{how}: {_group_names(groups)}) - setup-device.sh adds it to the "
            f"account and the unit grants it via SupplementaryGroups=; restart "
            f"visio-recorder afterwards"
        )
    elif node_groups != {requirement.group}:
        cause = (
            f"expected group {requirement.group} from Raspberry Pi OS's udev rules "
            f"(raspberrypi-sys-mods' 99-com.rules, systemd's 50-udev-default.rules) "
            f"- check those rules are installed and reboot"
        )
    else:
        cause = (
            f"{daemon.user} is in {requirement.group} but the mode does not grant "
            f"the group read-write"
        )
    return CheckResult(
        name=requirement.name,
        severity=requirement.severity,
        ok=False,
        detail=(
            f"{daemon.user} cannot open {described}, needed for "
            f"{requirement.needed_for}: {cause}"
        ),
    )


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
    daemon: DaemonIdentity | None = None,
) -> CheckResult:
    """Existence and writability answer different questions, so they differ.

    Whether the directory exists is caller-independent - /var/lib is 0755
    root:root, so a missing data dir looks missing to every uid - and a device
    without one is not provisioned: setup-device.sh exits at the env-file step
    before it ever reaches the data-dir step, both on a fresh device and
    whenever the Supabase keys are unfilled. That stays BLOCK for everyone.

    Writability is caller-relative, and only it. The daemon runs as the
    unit's user (``daemon``) and cannot function without a writable data dir,
    so for that caller an existing-but-unwritable directory stays BLOCK - the
    same gate the unit's ExecStartPre has always applied. ``os.access`` also
    refuses a path ProtectSystem=strict leaves read-only, so a data dir
    outside the unit's ReadWritePaths= blocks here rather than at the first
    segment. For an operator running the diagnostic over SSH it is
    *expected*: the directory is the daemon's own, mode 0750, because it holds
    recorded video, so reporting BLOCK made a healthy device look
    mis-provisioned and advised a re-run of setup-device.sh that would change
    nothing.

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
    if not exists:
        return CheckResult(
            name="data_dir",
            severity=BLOCK,
            ok=False,
            detail=f"{path} missing or not writable - run setup-device.sh{suffix}",
        )
    uid = getuid()
    daemon = resolve_daemon_identity() if daemon is None else daemon
    if uid == daemon.uid:
        return CheckResult(
            name="data_dir",
            severity=BLOCK,
            ok=False,
            detail=(
                f"{path} missing or not writable by {daemon.user} - run "
                f"setup-device.sh, which gives it to {daemon.user} and, for a "
                f"non-default path, adds it to the unit's ReadWritePaths={suffix}"
            ),
        )
    return CheckResult(
        name="data_dir",
        severity=WARN,
        ok=False,
        detail=(
            f"{path} exists but is not writable by uid {uid} - expected for a "
            f"diagnostic run as a non-daemon user, not a fault; the daemon runs "
            f"as {daemon.user}{suffix}"
        ),
    )


def _open_read_write(path: Path) -> None:
    os.close(os.open(path, os.O_RDWR))


def check_spi_device(
    path: Path = _SPI_DEVICE,
    getuid: Callable[[], int] = os.getuid,
    open_device: Callable[[Path], None] = _open_read_write,
    daemon: DaemonIdentity | None = None,
) -> CheckResult:
    """The LED driver's SPI node exists and opens read-write.

    Opened rather than stat'ed, because opening is what the driver does and
    what a missing ``spi`` group membership refuses. A missing node means SPI0
    is off for everyone; a refusal is caller-relative exactly as the data
    dir's writability is (see ``check_data_dir_writable``), so only the
    daemon's own uid is blocked by it. Whether the daemon's user could open it
    when someone else is asking is ``spi_access``'s question.
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
        daemon = resolve_daemon_identity() if daemon is None else daemon
        if uid == daemon.uid:
            return CheckResult(
                name="spidev0.0",
                severity=BLOCK,
                ok=False,
                detail=(
                    f"{path} exists but {daemon.user} (uid {uid}) cannot open it "
                    f"read-write - see spi_access"
                ),
            )
        return CheckResult(
            name="spidev0.0",
            severity=WARN,
            ok=False,
            detail=(
                f"{path} exists but uid {uid} cannot open it - add this user to the "
                f"spi group to drive the LED by hand; the daemon runs as "
                f"{daemon.user}, so this is not a fault (spi_access says whether "
                f"{daemon.user} can)"
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
    operator over SSH, since setup-device.sh writes it mode 0640 root:visio
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
                f"configured VISIO_DATA_DIR could not be confirmed - it is mode 0640 "
                f"root:visio, so re-run under sudo to check the configured path; if "
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


def device_requirements(
    battery_source: str, audio_device: str | None
) -> list[DeviceRequirement]:
    """The node classes the daemon will open in this configuration."""
    requirements = [CAMERA_NODES, LED_NODES, BUTTON_NODES]
    if audio_device is not None:
        requirements.append(MIC_NODES)
    if battery_source == "pijuice":
        requirements.append(I2C_NODES)
    return requirements


def run_checks(
    battery_source: str,
    data_dir: Path = _DATA_DIR,
    data_dir_caveat: str = "",
    audio_device: str | None = AudioSettings().device,
    daemon: DaemonIdentity | None = None,
) -> list[CheckResult]:
    daemon = resolve_daemon_identity() if daemon is None else daemon
    results = [
        check_daemon_user(daemon),
        check_binary_on_path("rpicam-vid"),
        check_binary_on_path("rpicam-still"),
        check_binary_on_path("zbarimg"),
        check_binary_on_path("ffmpeg"),
        check_binary_on_path("nmcli"),
        check_importable("spidev"),
        check_importable("gpiozero"),
        check_importable("supabase"),
        check_spi_device(daemon=daemon),
        check_core_freq_fixed(),
        check_camera_detected(),
        check_networkmanager_running(),
        check_data_dir_writable(path=data_dir, caveat=data_dir_caveat, daemon=daemon),
        check_audio_device(audio_device),
    ]
    if battery_source == "pijuice":
        results.append(check_pijuice_importable())
        results.append(check_i2c_enabled())
    results += [
        check_device_access(requirement, daemon)
        for requirement in device_requirements(battery_source, audio_device)
    ]
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
