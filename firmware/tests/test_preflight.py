"""Unit tests for visio_recorder.preflight's individual check functions.

Every check takes an injectable seam (a callable or a Path) defaulting to
the real implementation, so these run without touching real hardware -
the same fake-injection convention used throughout firmware/.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from visio_recorder import daemon
from visio_recorder.daemon import load_config
from visio_recorder.preflight import (
    _DAEMON_GROUPS,
    _DAEMON_USER,
    _DEFAULT_DATA_DIR,
    BLOCK,
    CAMERA_NODES,
    LED_NODES,
    MIC_NODES,
    WARN,
    CheckResult,
    DaemonIdentity,
    check_audio_device,
    check_binary_on_path,
    check_camera_detected,
    check_core_freq_fixed,
    check_daemon_user,
    check_data_dir_writable,
    check_device_access,
    check_i2c_enabled,
    check_importable,
    check_networkmanager_running,
    check_pijuice_importable,
    check_spi_device,
    format_report,
    has_blocking_failure,
    main,
    read_env_file,
    resolve_audio_device,
    resolve_battery_source,
    resolve_daemon_identity,
    resolve_data_dir,
    resolve_reported_data_dir,
    run_checks,
)

_OPERATOR_UID = 1000
_DAEMON_UID = 999
# The visio account as setup-device.sh creates it, for checks that compare the
# caller against the daemon without asking the host's passwd or systemd.
_DAEMON = DaemonIdentity(
    user="visio", uid=_DAEMON_UID, groups={_DAEMON_UID: "visio"}, source="test"
)
_UNIT = Path(__file__).resolve().parent.parent / "systemd" / "visio-recorder.service"


def test_check_binary_on_path_ok_when_found():
    result = check_binary_on_path("ffmpeg", which=lambda name: "/usr/bin/ffmpeg")

    assert result.name == "ffmpeg"
    assert result.severity == BLOCK
    assert result.ok is True


def test_check_binary_on_path_fails_when_missing():
    result = check_binary_on_path("ffmpeg", which=lambda name: None)

    assert result.ok is False
    assert result.severity == BLOCK
    assert "ffmpeg" in result.detail


def test_check_importable_ok_when_found():
    result = check_importable("supabase", find_spec=lambda mod: object())

    assert result.ok is True
    assert result.severity == BLOCK


def test_check_importable_fails_when_missing():
    result = check_importable("supabase", find_spec=lambda mod: None)

    assert result.ok is False


def test_check_pijuice_importable_is_warn_severity():
    result = check_pijuice_importable(find_spec=lambda mod: None)

    assert result.severity == WARN
    assert result.ok is False


def _completed(returncode: int, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


def test_check_camera_detected_ok_when_camera_listed():
    result = check_camera_detected(
        run=lambda args: _completed(
            0, "Available cameras\n-----------------\n0 : imx708_wide [4608x2592]\n"
        )
    )

    assert result.ok is True
    assert result.severity == BLOCK


def test_check_camera_detected_fails_when_none_listed():
    result = check_camera_detected(
        run=lambda args: _completed(0, "Available cameras\n-----------------\n")
    )

    assert result.ok is False


def test_check_camera_detected_fails_when_command_errors():
    result = check_camera_detected(run=lambda args: _completed(1, ""))

    assert result.ok is False


def test_check_networkmanager_running_ok_on_zero_exit():
    result = check_networkmanager_running(run=lambda args: _completed(0, "connected"))

    assert result.ok is True
    assert result.severity == BLOCK


def test_check_networkmanager_running_fails_on_nonzero_exit():
    result = check_networkmanager_running(run=lambda args: _completed(1, ""))

    assert result.ok is False


def test_check_data_dir_writable_ok_for_existing_writable_dir(tmp_path):
    result = check_data_dir_writable(path=tmp_path)

    assert result.ok is True
    assert result.severity == BLOCK


def test_check_data_dir_writable_fails_when_missing(tmp_path):
    result = check_data_dir_writable(path=tmp_path / "does-not-exist")

    assert result.ok is False


# --------------------------------------------------------------------------
# The data dir is root-owned on purpose - it holds recorded video - so the
# two halves of this check answer different questions. Existence is
# caller-independent (/var/lib is 0755 root:root), and a device without a
# data dir is simply not provisioned. Writability is the only caller-relative
# half, so it is the only one whose severity depends on who is asking.
#
# `can_write` is a seam for the same reason the other checks have one: the
# matrix below has to hold for every caller, and a test running as root
# cannot produce an unwritable directory with permission bits alone.
# --------------------------------------------------------------------------


def _unwritable(path):
    return False


def _uid(value):
    """A getuid seam bound to ``value`` rather than to a caller's loop variable.

    Closing over the loop variable directly happens to work while every call
    lands inside its own iteration, but it silently stops covering the daemon
    case the moment one does not.
    """
    return lambda: value


def test_check_data_dir_blocks_for_the_daemon_uid_when_missing(tmp_path):
    result = check_data_dir_writable(
        path=tmp_path / "missing", getuid=lambda: _DAEMON_UID, daemon=_DAEMON
    )

    assert result.ok is False
    assert result.severity == BLOCK
    assert "run setup-device.sh" in result.detail


def test_check_data_dir_blocks_a_non_daemon_caller_when_missing(tmp_path):
    """A missing data dir is not a permissions artefact - it blocks for everyone.

    setup-device.sh exits at the env-file step before it ever reaches the
    data-dir step, so "does not exist yet" is the normal state after a first
    run. Reporting that as an exit-0 warning would tell the operator the
    device is fine when the daemon's own ExecStartPre will refuse to start.
    """
    result = check_data_dir_writable(
        path=tmp_path / "missing", getuid=lambda: _OPERATOR_UID, daemon=_DAEMON
    )

    assert result.ok is False
    assert result.severity == BLOCK
    assert "run setup-device.sh" in result.detail


def test_check_data_dir_blocks_for_the_daemon_uid_when_unwritable(tmp_path):
    result = check_data_dir_writable(
        path=tmp_path, getuid=lambda: _DAEMON_UID, can_write=_unwritable, daemon=_DAEMON
    )

    assert result.ok is False
    assert result.severity == BLOCK
    assert "run setup-device.sh" in result.detail


def test_check_data_dir_only_warns_for_a_non_daemon_caller_when_unwritable(tmp_path):
    result = check_data_dir_writable(
        path=tmp_path,
        getuid=lambda: _OPERATOR_UID,
        can_write=_unwritable,
        daemon=_DAEMON,
    )

    assert result.ok is False
    assert result.severity == WARN
    # The advice that made the original report misleading: re-running setup
    # would not change anything for this caller.
    assert "setup-device.sh" not in result.detail
    assert "exists but is not writable" in result.detail
    assert "not a fault" in result.detail
    assert str(_OPERATOR_UID) in result.detail


def test_check_data_dir_is_ok_for_either_caller_when_writable(tmp_path):
    for uid in (_DAEMON_UID, _OPERATOR_UID):
        result = check_data_dir_writable(
            path=tmp_path, getuid=_uid(uid), daemon=_DAEMON
        )

        assert result.ok is True
        assert result.severity == BLOCK


@pytest.mark.skipif(
    os.getuid() == 0, reason="root ignores the permission bits this asserts on"
)
def test_the_default_can_write_seam_tracks_real_permission_bits(tmp_path):
    # Pins the default to os.access, so the fake above cannot drift from what
    # an operator's own uid actually sees on the device.
    root_owned = tmp_path / "visio-recorder"
    root_owned.mkdir(mode=0o500)

    result = check_data_dir_writable(
        path=root_owned, getuid=lambda: _OPERATOR_UID, daemon=_DAEMON
    )

    assert result.severity == WARN
    assert "exists but is not writable" in result.detail


def test_non_daemon_caller_on_a_healthy_device_exits_zero(
    tmp_path, monkeypatch, capsys
):
    """The exact false report: `visio-preflight` as a plain SSH user.

    Every other check passes and the data dir exists, only it is unwritable
    for this uid, and the run must not read as a provisioning failure.
    """
    healthy = [
        CheckResult(name="ffmpeg", severity=BLOCK, ok=True, detail="found on PATH"),
        check_data_dir_writable(
            path=tmp_path,
            getuid=lambda: _OPERATOR_UID,
            can_write=_unwritable,
            daemon=_DAEMON,
        ),
    ]
    monkeypatch.setattr(
        "visio_recorder.preflight.run_checks",
        lambda source, data_dir, caveat, **_: healthy,
    )

    exit_code = main()

    assert has_blocking_failure(healthy) is False
    assert exit_code == 0
    assert "[WARN] data_dir:" in capsys.readouterr().out


def test_daemon_uid_on_the_same_device_still_blocks_startup(tmp_path, monkeypatch):
    # The daemon-side gate is unchanged: the unit's ExecStartPre runs as the
    # daemon's uid and must still refuse to start.
    blocked = [
        check_data_dir_writable(
            path=tmp_path,
            getuid=lambda: _DAEMON_UID,
            can_write=_unwritable,
            daemon=_DAEMON,
        )
    ]
    monkeypatch.setattr(
        "visio_recorder.preflight.run_checks",
        lambda source, data_dir, caveat, **_: blocked,
    )

    assert has_blocking_failure(blocked) is True
    assert main() == 1


def test_a_missing_data_dir_blocks_startup_for_a_non_daemon_caller_too(
    tmp_path, monkeypatch
):
    # The regression this pins: an unprovisioned device must not report exit 0
    # just because the operator ran the diagnostic as themselves.
    unprovisioned = [
        check_data_dir_writable(
            path=tmp_path / "missing", getuid=lambda: _OPERATOR_UID, daemon=_DAEMON
        )
    ]
    monkeypatch.setattr(
        "visio_recorder.preflight.run_checks",
        lambda source, data_dir, caveat, **_: unprovisioned,
    )

    assert has_blocking_failure(unprovisioned) is True
    assert main() == 1


def _unit_directive(key: str) -> list[str]:
    return [
        line.strip().split("=", 1)[1]
        for line in _UNIT.read_text().splitlines()
        if line.strip().startswith(f"{key}=")
    ]


def test_the_daemon_user_matches_the_systemd_unit():
    """The packaged defaults are what an unprovisioned or CI run reports.

    Once the unit is installed its own values win, but these must still agree
    with the unit file, or a fresh device's preflight checks the wrong account.
    """
    assert _unit_directive("User") == [_DAEMON_USER]
    assert _unit_directive("Group") == [_DAEMON_USER]


def test_the_daemon_groups_match_the_units_supplementary_groups():
    assert [g.split() for g in _unit_directive("SupplementaryGroups")] == [
        list(_DAEMON_GROUPS)
    ]


class _Pw:
    def __init__(self, name, uid, gid):
        self.pw_name, self.pw_uid, self.pw_gid = name, uid, gid


class _Gr:
    def __init__(self, name, gid, members=()):
        self.gr_name, self.gr_gid, self.gr_mem = name, gid, list(members)


_HOST_GROUPS = [
    _Gr("visio", 999),
    _Gr("video", 44, ["visio", "pi"]),
    _Gr("gpio", 997, ["pi"]),
    _Gr("spi", 996, ["pi"]),
    _Gr("i2c", 995, ["pi"]),
    _Gr("audio", 29, ["pi"]),
    _Gr("netdev", 108, ["pi"]),
]


def _getpwnam(accounts):
    def lookup(name):
        if name not in accounts:
            raise KeyError(name)
        return accounts[name]

    return lookup


def test_the_installed_unit_decides_who_the_daemon_is():
    identity = resolve_daemon_identity(
        show=lambda: {
            "LoadState": "loaded",
            "User": "visio",
            "SupplementaryGroups": "gpio spi",
        },
        getpwnam=_getpwnam({"visio": _Pw("visio", 999, 999)}),
        getgrall=lambda: _HOST_GROUPS,
    )

    assert identity.uid == 999
    # The account's primary group and its own membership (video), plus what
    # the unit adds (gpio, spi) - and nothing the unit does not grant.
    assert sorted(identity.groups.values()) == ["gpio", "spi", "video", "visio"]
    assert "installed" in identity.source


def test_a_rollback_drop_in_to_root_is_reported_as_root():
    # `systemctl show` prints an empty User= for a unit that runs as root.
    identity = resolve_daemon_identity(
        show=lambda: {"LoadState": "loaded", "User": "", "SupplementaryGroups": ""},
        getpwnam=_getpwnam({"root": _Pw("root", 0, 0)}),
        getgrall=lambda: [_Gr("root", 0)],
    )

    assert (identity.user, identity.uid) == ("root", 0)


def test_an_uninstalled_unit_falls_back_to_the_packaged_defaults():
    for show in (lambda: None, lambda: {"LoadState": "not-found", "User": ""}):
        identity = resolve_daemon_identity(
            show=show,
            getpwnam=_getpwnam({"visio": _Pw("visio", 999, 999)}),
            getgrall=lambda: _HOST_GROUPS,
        )

        assert identity.user == _DAEMON_USER
        assert set(identity.groups.values()) == {"visio", *_DAEMON_GROUPS}


def test_a_missing_daemon_account_blocks_and_names_the_fix():
    identity = resolve_daemon_identity(
        show=lambda: None, getpwnam=_getpwnam({}), getgrall=lambda: _HOST_GROUPS
    )

    result = check_daemon_user(identity)

    assert identity.uid is None
    assert result.ok is False and result.severity == BLOCK
    assert "'visio'" in result.detail and "setup-device.sh" in result.detail


def test_an_existing_daemon_account_lists_its_groups():
    result = check_daemon_user(
        DaemonIdentity("visio", 999, {999: "visio", 44: "video"}, "test")
    )

    assert result.ok is True
    assert "uid 999" in result.detail and "video visio" in result.detail


def test_check_i2c_enabled_ok_when_device_exists(tmp_path):
    fake_i2c = tmp_path / "i2c-1"
    fake_i2c.touch()

    result = check_i2c_enabled(path=fake_i2c)

    assert result.ok is True
    assert result.severity == WARN


def test_check_i2c_enabled_fails_when_device_missing(tmp_path):
    result = check_i2c_enabled(path=tmp_path / "i2c-1")

    assert result.ok is False
    assert result.severity == WARN


def _raise(exc):
    raise exc


def test_check_camera_detected_fails_when_binary_missing():
    result = check_camera_detected(
        run=lambda args: _raise(FileNotFoundError("rpicam-hello not found"))
    )

    assert result.ok is False
    assert result.severity == BLOCK
    assert "rpicam-hello" in result.detail
    assert "unavailable" in result.detail


def test_check_camera_detected_fails_when_command_times_out():
    result = check_camera_detected(
        run=lambda args: _raise(subprocess.TimeoutExpired(cmd=args, timeout=5))
    )

    assert result.ok is False
    assert result.severity == BLOCK
    assert "timeout" in result.detail


def test_check_camera_detected_fails_when_permission_denied():
    result = check_camera_detected(
        run=lambda args: _raise(PermissionError("permission denied"))
    )

    assert result.ok is False
    assert result.severity == BLOCK
    assert "unavailable" in result.detail


def test_check_networkmanager_running_fails_when_binary_missing():
    result = check_networkmanager_running(
        run=lambda args: _raise(FileNotFoundError("nmcli not found"))
    )

    assert result.ok is False
    assert result.severity == BLOCK
    assert "nmcli" in result.detail
    assert "unavailable" in result.detail


def test_check_networkmanager_running_fails_when_command_times_out():
    result = check_networkmanager_running(
        run=lambda args: _raise(subprocess.TimeoutExpired(cmd=args, timeout=5))
    )

    assert result.ok is False
    assert result.severity == BLOCK
    assert "timeout" in result.detail


def test_check_networkmanager_running_fails_when_permission_denied():
    result = check_networkmanager_running(
        run=lambda args: _raise(PermissionError("permission denied"))
    )

    assert result.ok is False
    assert result.severity == BLOCK
    assert "unavailable" in result.detail


def test_run_checks_includes_pijuice_and_i2c_when_battery_source_is_pijuice():
    names = {r.name for r in run_checks("pijuice")}

    assert "pijuice" in names
    assert "i2c" in names


def test_run_checks_excludes_pijuice_and_i2c_when_battery_source_is_none():
    names = {r.name for r in run_checks("none")}

    assert "pijuice" not in names
    assert "i2c" not in names


def test_has_blocking_failure_true_when_a_block_check_fails():
    results = [
        CheckResult(name="a", severity=BLOCK, ok=False, detail="x"),
        CheckResult(name="b", severity=WARN, ok=True, detail="y"),
    ]

    assert has_blocking_failure(results) is True


def test_has_blocking_failure_false_when_only_warn_checks_fail():
    results = [
        CheckResult(name="a", severity=BLOCK, ok=True, detail="x"),
        CheckResult(name="b", severity=WARN, ok=False, detail="y"),
    ]

    assert has_blocking_failure(results) is False


def test_format_report_marks_failures_with_severity_and_passes_as_ok():
    results = [
        CheckResult(name="a", severity=BLOCK, ok=True, detail="fine"),
        CheckResult(name="b", severity=WARN, ok=False, detail="missing"),
    ]

    report = format_report(results)

    assert "[OK] a: fine" in report
    assert "[WARN] b: missing" in report


# --------------------------------------------------------------------------
# The data dir preflight gates on has to be the one the daemon records into.
# `VISIO_DATA_DIR` is documented configuration the unit's EnvironmentFile makes
# reachable, so a hardcoded /var/lib/visio-recorder would report [OK] for a
# directory the daemon never touches while it writes somewhere else.
# --------------------------------------------------------------------------


def test_resolve_data_dir_defaults_to_the_packaged_location():
    assert resolve_data_dir({}) == Path(_DEFAULT_DATA_DIR)


def test_resolve_data_dir_honours_the_configured_override():
    assert resolve_data_dir({"VISIO_DATA_DIR": "/mnt/usb/visio"}) == Path(
        "/mnt/usb/visio"
    )


def test_resolve_data_dir_matches_daemon_load_config_exactly():
    """Same key, same default, same precedence - including the odd cases.

    ``.get(key, default)`` and ``.get(key) or default`` differ on an empty
    value, and an env file carrying ``VISIO_DATA_DIR=`` gives systemd exactly
    that. Whatever the daemon does with it, the gate has to agree, or it is
    green about a directory the daemon is not using.
    """
    for env in ({}, {"VISIO_DATA_DIR": "/mnt/usb/visio"}, {"VISIO_DATA_DIR": ""}):
        expected = load_config(
            {"SUPABASE_URL": "u", "SUPABASE_ANON_KEY": "k", **env}
        ).data_dir

        assert resolve_data_dir(env) == expected


def test_preflight_and_daemon_share_one_default_data_dir():
    # daemon.load_config resolves through preflight.resolve_data_dir rather
    # than carrying its own constant, so there is nothing left to drift. A
    # second default reappearing here is exactly how that would come back.
    assert not hasattr(daemon, "_DEFAULT_DATA_DIR"), (
        "daemon must not define a competing data-dir default; resolve_data_dir owns it"
    )
    assert load_config(
        {"SUPABASE_URL": "u", "SUPABASE_ANON_KEY": "k"}
    ).data_dir == Path(_DEFAULT_DATA_DIR)


def test_run_checks_gates_on_the_data_dir_it_is_given(tmp_path):
    configured = tmp_path / "usb-mount"
    configured.mkdir()

    detail = next(
        r.detail for r in run_checks("none", configured) if r.name == "data_dir"
    )

    assert str(configured) in detail


def test_run_checks_falls_back_to_the_default_data_dir():
    detail = next(r.detail for r in run_checks("none") if r.name == "data_dir")

    assert _DEFAULT_DATA_DIR in detail


def test_main_checks_the_configured_data_dir(monkeypatch, capsys):
    seen = []

    def record(source, data_dir, caveat, **_):
        seen.append(data_dir)
        return []

    monkeypatch.setenv("VISIO_DATA_DIR", "/mnt/usb/visio")
    monkeypatch.setattr("visio_recorder.preflight.run_checks", record)

    main()
    capsys.readouterr()

    assert seen == [Path("/mnt/usb/visio")]


# --------------------------------------------------------------------------
# Remediation advice. preflight is the operator's entry point, so pointing at
# the wrong step costs real bring-up time: only supabase comes from uv sync,
# gpiozero is apt's python3-gpiozero reaching the venv through
# --system-site-packages, and so is spidev (python3-spidev).
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        ("spidev", "python3-spidev"),
        ("gpiozero", "python3-gpiozero"),
        ("supabase", "uv sync"),
    ],
)
def test_a_failed_import_names_the_step_that_actually_installs_it(module, expected):
    result = check_importable(module, find_spec=lambda mod: None)

    assert expected in result.detail
    assert result.severity == BLOCK


def test_no_module_is_advised_to_check_a_step_that_does_not_install_it():
    for module in ("spidev", "gpiozero"):
        detail = check_importable(module, find_spec=lambda mod: None).detail

        assert "uv sync" not in detail, f"{module} does not come from uv sync"


def test_an_unrecognised_module_still_gets_the_uv_sync_default():
    result = check_importable("some-future-dependency", find_spec=lambda mod: None)

    assert "check the uv sync step" in result.detail


# --------------------------------------------------------------------------
# Which data dir gets checked is caller-relative too. ExecStartPre inherits
# VISIO_DATA_DIR from the unit's EnvironmentFile=; the standalone
# visio-preflight inherits nothing, so it reads /etc/visio-recorder.env
# itself - and that file is mode 600 root-owned and stays that way, so an
# unprivileged run genuinely cannot know the configured value. Reporting the
# default as though it were the configured one shows a blocking failure for a
# directory that is correctly absent on a device recording to a USB mount.
# --------------------------------------------------------------------------


def _env_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "visio-recorder.env"
    path.write_text(text)
    return path


def test_read_env_file_parses_it_the_way_systemd_does(tmp_path):
    env_file = _env_file(
        tmp_path,
        "# a comment\n"
        "SUPABASE_URL=https://x.supabase.co\n"
        'VISIO_DATA_DIR="/mnt/usb/visio"\n'
        "VISIO_BATTERY_SOURCE= none \n"
        "# VISIO_DATA_DIR=/commented/out\n"
        "VISIO_DATA_DIR=/last/assignment/wins\n"
        "not a key=value line\n",
    )

    values = read_env_file(env_file)

    # None is the distinct "could not read it" outcome; a readable file must
    # never report that.
    assert values is not None
    assert values["VISIO_DATA_DIR"] == "/last/assignment/wins"
    assert values["VISIO_BATTERY_SOURCE"] == "none"
    assert values["SUPABASE_URL"] == "https://x.supabase.co"


def test_read_env_file_strips_one_layer_of_matching_quotes(tmp_path):
    # Quoting is idiomatic in an EnvironmentFile, and a literal directory
    # named '"/mnt/usb"' would be a silent mis-check.
    values = read_env_file(_env_file(tmp_path, 'VISIO_DATA_DIR="/mnt/usb/visio"\n'))

    assert values is not None
    assert values["VISIO_DATA_DIR"] == "/mnt/usb/visio"


def test_read_env_file_reports_an_absent_file_as_nothing_configured(tmp_path):
    assert read_env_file(tmp_path / "nothing-here.env") == {}


@pytest.mark.skipif(
    os.getuid() == 0, reason="root ignores the permission bits this asserts on"
)
def test_read_env_file_returns_none_rather_than_raising_on_the_real_0600_file(tmp_path):
    # The operator case, against real permission bits rather than a fake: the
    # device's env file is mode 600 root-owned, and a raise here would abort
    # visio-preflight instead of degrading to the documented caveat.
    unreadable = _env_file(tmp_path, "VISIO_DATA_DIR=/mnt/usb/visio\n")
    unreadable.chmod(0o000)

    assert read_env_file(unreadable) is None


def test_an_environment_variable_beats_the_env_file(tmp_path):
    # ExecStartPre's path: systemd already applied EnvironmentFile=, so the
    # process environment is authoritative and no file read should override it.
    env_file = _env_file(tmp_path, "VISIO_DATA_DIR=/from/the/file\n")

    resolution = resolve_reported_data_dir(
        {"VISIO_DATA_DIR": "/from/the/environment"}, env_file
    )

    assert resolution.path == Path("/from/the/environment")
    assert resolution.caveat == ""


def test_a_readable_env_file_supplies_the_configured_data_dir(tmp_path):
    # The root operator's path: `sudo visio-preflight` can read the file, so
    # it gets the same answer the daemon will.
    env_file = _env_file(tmp_path, "VISIO_DATA_DIR=/mnt/usb/visio\n")

    resolution = resolve_reported_data_dir({}, env_file)

    assert resolution.path == Path("/mnt/usb/visio")
    assert resolution.caveat == ""


def test_an_absent_env_file_is_the_default_with_no_caveat(tmp_path):
    resolution = resolve_reported_data_dir({}, tmp_path / "never-written.env")

    assert resolution.path == Path(_DEFAULT_DATA_DIR)
    assert resolution.caveat == "", "nothing configured means the default IS the truth"


def test_an_unreadable_env_file_says_so_instead_of_guessing(tmp_path):
    def denied(path):
        return None

    resolution = resolve_reported_data_dir({}, Path("/etc/visio-recorder.env"), denied)

    assert resolution.path == Path(_DEFAULT_DATA_DIR)
    assert "/etc/visio-recorder.env" in resolution.caveat
    assert "could not be confirmed" in resolution.caveat
    assert "sudo" in resolution.caveat


def test_the_caveat_reaches_the_report_without_changing_any_severity(tmp_path):
    caveat = "could not be confirmed"

    writable = check_data_dir_writable(path=tmp_path, caveat=caveat)
    missing = check_data_dir_writable(path=tmp_path / "gone", caveat=caveat)
    unwritable = check_data_dir_writable(
        path=tmp_path,
        getuid=lambda: _OPERATOR_UID,
        can_write=_unwritable,
        caveat=caveat,
        daemon=_DAEMON,
    )

    for result in (writable, missing, unwritable):
        assert caveat in result.detail
    assert writable.severity == BLOCK and writable.ok is True
    assert missing.severity == BLOCK and missing.ok is False
    assert unwritable.severity == WARN and unwritable.ok is False


def test_the_caveat_is_absent_when_the_value_was_confirmed(tmp_path):
    assert "[" not in check_data_dir_writable(path=tmp_path).detail


def test_an_unprivileged_run_reports_the_caveat_end_to_end(
    tmp_path, monkeypatch, capsys
):
    """The operator sequence the caveat exists for.

    A device recording to a USB mount, `visio-preflight` over SSH: the default
    is genuinely missing, so this still blocks - but the report says why the
    directory it checked may not be the configured one, instead of sending the
    operator to re-run setup-device.sh, which would not create it either.
    """
    monkeypatch.delenv("VISIO_DATA_DIR", raising=False)
    monkeypatch.setattr("visio_recorder.preflight.read_env_file", lambda path: None)
    monkeypatch.setattr(
        "visio_recorder.preflight.run_checks",
        lambda source, data_dir, caveat, **_: [
            check_data_dir_writable(path=data_dir, caveat=caveat)
        ],
    )

    exit_code = main()

    out = capsys.readouterr().out
    assert exit_code == 1
    assert _DEFAULT_DATA_DIR in out
    assert "could not be confirmed" in out


def test_the_battery_source_is_resolved_the_same_way(tmp_path):
    # Same blind spot, same fix - only WARN checks depend on it, so it carries
    # no caveat, but a root run should still see what the daemon will.
    env_file = _env_file(tmp_path, "VISIO_BATTERY_SOURCE=none\n")

    assert resolve_battery_source({}, env_file) == "none"
    assert (
        resolve_battery_source({"VISIO_BATTERY_SOURCE": "pijuice"}, env_file)
        == "pijuice"
    )
    assert resolve_battery_source({}, tmp_path / "absent.env") == "pijuice"
    assert (
        resolve_battery_source({}, tmp_path / "x.env", lambda path: None) == "pijuice"
    )


def test_read_env_file_accepts_an_indented_key_the_way_systemd_does(tmp_path):
    # Pinned on both sides: setup-device.sh's env_file_value tolerates the
    # same leading whitespace, so the script cannot provision one directory
    # while systemd hands the daemon another.
    values = read_env_file(_env_file(tmp_path, "  VISIO_DATA_DIR=/mnt/usb/visio\n"))

    assert values is not None
    assert values["VISIO_DATA_DIR"] == "/mnt/usb/visio"


def test_read_env_file_degrades_instead_of_raising_on_undecodable_bytes(tmp_path):
    # systemd tolerates arbitrary bytes in an EnvironmentFile, so the daemon
    # starts fine; a traceback out of the diagnostic that exists to explain
    # such a device would be worse than any report it could print.
    corrupt = tmp_path / "visio-recorder.env"
    corrupt.write_bytes(b"VISIO_DATA_DIR=/mnt/\xff\xfe/visio\n")

    assert read_env_file(corrupt) is None


def test_an_undecodable_env_file_produces_a_caveat_not_a_traceback(tmp_path):
    corrupt = tmp_path / "visio-recorder.env"
    corrupt.write_bytes(b"VISIO_DATA_DIR=/mnt/\xff\xfe/visio\n")

    resolution = resolve_reported_data_dir({}, corrupt)

    assert resolution.path == Path(_DEFAULT_DATA_DIR)
    assert "could not be read or parsed" in resolution.caveat


def test_read_env_file_does_not_depend_on_the_ambient_locale(tmp_path):
    # Under systemd the unit inherits whatever locale it was given, routinely
    # C/POSIX. Decoding a valid UTF-8 path as ASCII would fail on a file that
    # is not corrupt at all, which is why read_env_file pins encoding="utf-8".
    #
    # This has to run in a subprocess. CPython calls setlocale(LC_CTYPE, "")
    # once at interpreter startup, so setting LC_ALL from inside the test does
    # not change what Path.read_text() decodes with - an in-process version of
    # this test passes whether or not the pin is present, and so guards
    # nothing. PYTHONCOERCECLOCALE and PYTHONUTF8 are disabled as well, or PEP
    # 538/540 would quietly restore UTF-8 and hide the failure being guarded.
    env_file = tmp_path / "visio-recorder.env"
    env_file.write_bytes("VISIO_DATA_DIR=/mnt/donn\xe9es/visio\n".encode())

    # The expected value is spelled as an ASCII escape inside the probe rather
    # than passed as an argument: under LC_ALL=C with PYTHONUTF8=0, encoding a
    # non-ASCII argv entry fails in the parent before the child even starts.
    # The comparison happens in the child for the same reason - printing the
    # path back would fail on an ASCII stdout and look like the bug under test.
    probe = (
        "import pathlib, sys;"
        "from visio_recorder.preflight import read_env_file;"
        "values = read_env_file(pathlib.Path(sys.argv[1]));"
        "print('MATCH' if values is not None and"
        " values.get('VISIO_DATA_DIR') == '/mnt/donn\\xe9es/visio'"
        " else 'MISMATCH')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, str(env_file)],
        env={
            **os.environ,
            "LC_ALL": "C",
            "LANG": "C",
            "PYTHONCOERCECLOCALE": "0",
            "PYTHONUTF8": "0",
        },
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "MATCH"


# --------------------------------------------------------------------------
# The SPI status LED (#85). The driver writes WS2812B frames to
# /dev/spidev0.0 through py-spidev, so preflight blocks on the module, on the
# device node (absent when dtparam=spi=on is missing or not yet rebooted
# into), and on it opening for the daemon's uid. Opening is caller-relative
# the way the data dir's writability is: an operator outside the spi group
# gets a warning, not a provisioning failure.
# --------------------------------------------------------------------------


def _open_raising(exc: BaseException):
    def open_device(path: Path) -> None:
        raise exc

    return open_device


def test_run_checks_blocks_on_spidev_not_rpi_ws281x():
    names = {r.name for r in run_checks("none")}

    assert "spidev" in names
    assert "spidev0.0" in names
    assert "rpi_ws281x" not in names


def test_spi_device_that_opens_is_ok(tmp_path):
    result = check_spi_device(path=tmp_path / "spidev0.0", open_device=lambda p: None)

    assert result.ok is True
    assert result.severity == BLOCK


def test_a_missing_spi_device_blocks_every_caller_and_names_the_fix(tmp_path):
    for uid in (_DAEMON_UID, _OPERATOR_UID):
        result = check_spi_device(
            path=tmp_path / "spidev0.0",
            getuid=_uid(uid),
            open_device=_open_raising(FileNotFoundError(2, "No such file")),
            daemon=_DAEMON,
        )

        assert result.ok is False
        assert result.severity == BLOCK
        assert "dtparam=spi=on" in result.detail
        assert "reboot" in result.detail


def test_an_spi_device_the_daemon_cannot_open_blocks(tmp_path):
    result = check_spi_device(
        path=tmp_path / "spidev0.0",
        getuid=lambda: _DAEMON_UID,
        open_device=_open_raising(PermissionError(13, "Permission denied")),
        daemon=_DAEMON,
    )

    assert result.ok is False
    assert result.severity == BLOCK


def test_an_operator_outside_the_spi_group_only_gets_a_warning(tmp_path):
    result = check_spi_device(
        path=tmp_path / "spidev0.0",
        getuid=lambda: _OPERATOR_UID,
        open_device=_open_raising(PermissionError(13, "Permission denied")),
        daemon=_DAEMON,
    )

    assert result.ok is False
    assert result.severity == WARN
    assert "spi group" in result.detail


def test_the_default_open_seam_really_opens_the_node(tmp_path):
    node = tmp_path / "spidev0.0"
    node.write_bytes(b"")

    assert check_spi_device(path=node).ok is True
    assert check_spi_device(path=tmp_path / "absent").ok is False


@pytest.mark.parametrize(
    ("stdout", "ok"),
    [("core_freq_fixed=1\n", True), ("core_freq_fixed=0\n", False), ("", False)],
)
def test_core_freq_fixed_is_read_from_the_firmware_config(stdout, ok):
    result = check_core_freq_fixed(
        run=lambda args: subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
    )

    assert result.ok is ok
    assert result.severity == WARN, (
        "a scaling clock skews colours, it does not stop recording"
    )
    if not ok:
        assert "core_freq_fixed=1" in result.detail


def test_core_freq_fixed_without_vcgencmd_warns_rather_than_crashing():
    def missing(args):
        raise FileNotFoundError("vcgencmd")

    result = check_core_freq_fixed(run=missing)

    assert result.ok is False
    assert result.severity == WARN


# --------------------------------------------------------------------------
# The I2S microphone (#87). A pendant without a working microphone records
# video only, so every outcome here is WARN - preflight must never stop the
# daemon over audio. What it adds over the daemon's own startup probe is the
# diagnosis: which of the overlay, the ALSA config and the wiring is missing.
# --------------------------------------------------------------------------

_ADAU_CARDS = (
    " 0 [adau7002       ]: adau7002 - adau7002\n                      adau7002\n"
)


def _audio(
    tmp_path: Path,
    *,
    opens: bool,
    cards: str = _ADAU_CARDS,
    asound: str | None = "pcm.visio_mic {\n    type plug\n}\n",
    device: str | None = "visio_mic",
):
    cards_path = tmp_path / "cards"
    cards_path.write_text(cards)
    asound_path = tmp_path / "asound.conf"
    if asound is not None:
        asound_path.write_text(asound)
    probed: list[str] = []

    def probe(name: str) -> bool:
        probed.append(name)
        return opens

    result = check_audio_device(
        device, probe=probe, cards_path=cards_path, asound_conf=asound_path
    )
    return result, probed


def test_a_microphone_that_opens_is_ok(tmp_path):
    result, probed = _audio(tmp_path, opens=True)

    assert result.ok is True
    assert result.severity == WARN
    assert result.name == "audio"
    assert probed == ["visio_mic"]


def test_a_missing_i2s_card_points_at_the_overlay(tmp_path):
    result, _ = _audio(
        tmp_path, opens=False, cards=" 0 [vc4hdmi        ]: vc4-hdmi - vc4-hdmi\n"
    )

    assert result.ok is False
    assert result.severity == WARN
    assert "dtoverlay=adau7002-simple" in result.detail
    assert "video only" in result.detail


def test_a_missing_pcm_definition_points_at_the_alsa_config(tmp_path):
    result, _ = _audio(tmp_path, opens=False, asound=None)

    assert result.severity == WARN
    assert "firmware/alsa/visio-mic.conf" in result.detail
    assert "/etc/asound.conf" in result.detail or str(tmp_path) in result.detail


def test_card_and_config_present_but_no_open_points_at_wiring(tmp_path):
    result, _ = _audio(tmp_path, opens=False)

    assert result.severity == WARN
    assert "wiring" in result.detail
    assert "in use" in result.detail, "a recording daemon holds the device"


def test_audio_off_is_not_probed(tmp_path):
    result, probed = _audio(tmp_path, opens=False, device=None)

    assert result.ok is True
    assert probed == []
    assert "VISIO_AUDIO=off" in result.detail


def test_no_audio_outcome_can_block_startup(tmp_path):
    for kwargs in (
        {"opens": False},
        {"opens": False, "asound": None},
        {"opens": False, "cards": ""},
    ):
        result, _ = _audio(tmp_path, **kwargs)
        assert has_blocking_failure([result]) is False


def test_the_audio_check_reuses_the_daemons_own_probe(monkeypatch):
    # One definition of "the microphone opens": the ffmpeg capture the daemon
    # runs at startup, so preflight and the daemon cannot disagree.
    from visio_recorder import capture, preflight

    calls: list[capture.AudioSettings] = []

    def probe(runner, audio: capture.AudioSettings) -> bool:
        calls.append(audio)
        return True

    monkeypatch.setattr(preflight, "probe_audio_device", probe)

    result = check_audio_device("visio_mic")

    assert result.ok is True
    assert calls == [capture.AudioSettings(device="visio_mic")]


@pytest.mark.parametrize(
    ("env", "file_values", "expected"),
    [
        ({}, {}, "visio_mic"),
        ({"VISIO_AUDIO": "off"}, {}, None),
        ({}, {"VISIO_AUDIO": "OFF"}, None),
        ({}, {"VISIO_AUDIO_DEVICE": "hw:1"}, "hw:1"),
        (
            {"VISIO_AUDIO_DEVICE": "plughw:2"},
            {"VISIO_AUDIO_DEVICE": "hw:1"},
            "plughw:2",
        ),
    ],
)
def test_resolve_audio_device_matches_the_daemon(tmp_path, env, file_values, expected):
    assert resolve_audio_device(env, read_file=lambda path: file_values) == expected


def test_run_checks_reports_audio_without_blocking(monkeypatch):
    from visio_recorder import preflight

    monkeypatch.setattr(preflight, "probe_audio_device", lambda runner, audio: False)

    results = run_checks("none", audio_device="visio_mic")
    audio = next(r for r in results if r.name == "audio")

    assert audio.ok is False
    assert audio.severity == WARN


def test_the_real_probe_keeps_ffmpegs_stderr_out_of_the_report(capfd):
    # On any machine without the I2S card the probe fails; what matters is
    # that ffmpeg's and ALSA's own error lines do not interleave with the
    # [OK]/[WARN] report an operator reads, or flood journalctl.
    result = check_audio_device("visio_mic_test_device_that_does_not_exist")

    assert result.ok is False
    captured = capfd.readouterr()
    assert captured.err == ""
    assert captured.out == ""


# --------------------------------------------------------------------------
# Device access for the daemon's user. These answer "can visio open it" for
# every caller, so an operator over SSH - or root under sudo, who can open
# anything - still learns what the daemon will hit, by node and by group.
# --------------------------------------------------------------------------

_GIDS = {0: "root", 44: "video", 29: "audio", 996: "spi", 997: "gpio", 999: "visio"}


def _node(mode: int, uid: int = 0, gid: int = 44, kind: int = 0o020000):
    return os.stat_result((kind | mode, 1, 1, 1, uid, gid, 0, 0, 0, 0))


def _access_check(requirement, nodes, daemon, getuid=lambda: _OPERATOR_UID, **seams):
    return check_device_access(
        requirement,
        daemon,
        getuid=getuid,
        glob=lambda pattern: [p for p in nodes if Path(p).match(pattern)],
        stat_node=lambda path: nodes[path],
        group_name=lambda gid: _GIDS.get(gid, str(gid)),
        owner_name=lambda uid: "root" if uid == 0 else str(uid),
        **seams,
    )


_CAMERA = {
    "/dev/media0": _node(0o660),
    "/dev/video0": _node(0o660),
    "/dev/v4l-subdev0": _node(0o660),
    "/dev/dma_heap/linux,cma": _node(0o660),
    "/dev/dma_heap/system": _node(0o660),
}
_VISIO_IN_VIDEO = DaemonIdentity("visio", 999, {999: "visio", 44: "video"}, "test")
_VISIO_ALONE = DaemonIdentity("visio", 999, {999: "visio"}, "test")


def test_the_daemon_in_the_video_group_can_open_the_camera_nodes():
    result = _access_check(CAMERA_NODES, _CAMERA, _VISIO_IN_VIDEO)

    assert result.ok is True
    assert "5 node(s)" in result.detail


def test_a_daemon_missing_the_video_group_is_blocked_by_name():
    result = _access_check(CAMERA_NODES, _CAMERA, _VISIO_ALONE)

    assert result.ok is False and result.severity == BLOCK
    assert "/dev/dma_heap/system (root:video 0660)" in result.detail
    assert "visio is not in the video group" in result.detail
    assert "SupplementaryGroups=" in result.detail


def test_a_node_owned_by_the_wrong_group_points_at_the_udev_rules():
    # The account is right; the node is not what Raspberry Pi OS's rules make.
    nodes = {"/dev/spidev0.0": _node(0o600, gid=0)}
    daemon = DaemonIdentity("visio", 999, {999: "visio", 996: "spi"}, "test")

    result = _access_check(LED_NODES, nodes, daemon)

    assert result.ok is False and result.severity == BLOCK
    assert "/dev/spidev0.0 (root:root 0600)" in result.detail
    assert "expected group spi" in result.detail
    assert "99-com.rules" in result.detail


def test_root_under_sudo_still_gets_the_daemons_answer():
    # Root can open every node itself; the report must not pass on its behalf.
    result = _access_check(CAMERA_NODES, _CAMERA, _VISIO_ALONE, getuid=lambda: 0)

    assert result.ok is False


def test_as_the_daemon_itself_the_kernel_is_asked_with_the_real_groups():
    asked = []

    def access(path):
        asked.append(path)
        return path != "/dev/video0"

    result = _access_check(
        CAMERA_NODES,
        _CAMERA,
        _VISIO_IN_VIDEO,
        getuid=lambda: 999,
        getgroups=lambda: [999],
        access=access,
    )

    assert sorted(asked) == sorted(_CAMERA)
    assert result.ok is False
    # The process's own groups, not the account's: here video is missing.
    assert "visio is not in the video group" in result.detail
    assert "this process" in result.detail


def test_directories_under_a_pattern_are_not_nodes():
    nodes = {
        "/dev/snd/controlC0": _node(0o660, gid=29),
        "/dev/snd/by-path": _node(0o755, kind=0o040000),
    }
    daemon = DaemonIdentity("visio", 999, {999: "visio", 29: "audio"}, "test")

    result = _access_check(MIC_NODES, nodes, daemon)

    assert result.ok is True and "1 node(s)" in result.detail
    assert result.severity == WARN


def test_no_nodes_at_all_is_reported_rather_than_passed():
    result = _access_check(LED_NODES, {}, _VISIO_IN_VIDEO)

    assert result.ok is False and result.severity == BLOCK
    assert "/dev/spidev0.0" in result.detail


def test_a_missing_account_cannot_be_checked_and_says_so():
    missing = DaemonIdentity("visio", None, {}, "test")

    result = _access_check(CAMERA_NODES, _CAMERA, missing)

    assert result.ok is False and "does not exist" in result.detail


def test_the_owner_bits_apply_to_a_node_the_daemon_owns():
    nodes = {"/dev/spidev0.0": _node(0o600, uid=999, gid=0)}

    assert _access_check(LED_NODES, nodes, _VISIO_ALONE).ok is True


def test_the_default_seams_read_real_permission_bits(tmp_path):
    # A regular file standing in for a node: the stat-based path for another
    # account, against real st_mode/st_gid values.
    node = tmp_path / "spidev0.0"
    node.write_bytes(b"")
    node.chmod(0o660)
    gid = node.stat().st_gid
    requirement = LED_NODES.__class__(
        name="spi_access", patterns=(str(node),), group="spi", severity=BLOCK,
        needed_for="the status LED",
    )  # fmt: skip
    member = DaemonIdentity("visio", 64999, {gid: "spi"}, "test")
    outsider = DaemonIdentity("visio", 64999, {64998: "visio"}, "test")

    assert check_device_access(requirement, member, getuid=lambda: -1).ok is True
    assert check_device_access(requirement, outsider, getuid=lambda: -1).ok is False


def test_run_checks_covers_every_node_class_the_configuration_uses(monkeypatch):
    from visio_recorder import preflight

    monkeypatch.setattr(preflight, "probe_audio_device", lambda runner, audio: False)

    with_all = {r.name for r in run_checks("pijuice", daemon=_DAEMON)}
    minimal = {r.name for r in run_checks("none", audio_device=None, daemon=_DAEMON)}

    assert {"daemon_user", "camera_access", "spi_access", "gpio_access"} <= minimal
    assert {"audio_access", "i2c_access"} <= with_all
    assert not {"audio_access", "i2c_access"} & minimal
