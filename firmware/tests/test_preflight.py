"""Unit tests for visio_recorder.preflight's individual check functions.

Every check takes an injectable seam (a callable or a Path) defaulting to
the real implementation, so these run without touching real hardware -
the same fake-injection convention used throughout firmware/.
"""

import os
import subprocess
from pathlib import Path

import pytest

from visio_recorder.daemon import _DEFAULT_DATA_DIR as _DAEMON_DEFAULT_DATA_DIR
from visio_recorder.daemon import load_config
from visio_recorder.preflight import (
    BLOCK,
    WARN,
    CheckResult,
    _DAEMON_UID,
    _DEFAULT_DATA_DIR,
    check_binary_on_path,
    check_camera_detected,
    check_data_dir_writable,
    check_i2c_enabled,
    check_importable,
    check_networkmanager_running,
    check_pijuice_importable,
    format_report,
    has_blocking_failure,
    main,
    resolve_data_dir,
    run_checks,
)

_OPERATOR_UID = 1000
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
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


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


def test_check_data_dir_blocks_for_the_daemon_uid_when_missing(tmp_path):
    result = check_data_dir_writable(path=tmp_path / "missing", getuid=lambda: _DAEMON_UID)

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
    result = check_data_dir_writable(path=tmp_path / "missing", getuid=lambda: _OPERATOR_UID)

    assert result.ok is False
    assert result.severity == BLOCK
    assert "run setup-device.sh" in result.detail


def test_check_data_dir_blocks_for_the_daemon_uid_when_unwritable(tmp_path):
    result = check_data_dir_writable(
        path=tmp_path, getuid=lambda: _DAEMON_UID, can_write=_unwritable
    )

    assert result.ok is False
    assert result.severity == BLOCK
    assert "run setup-device.sh" in result.detail


def test_check_data_dir_only_warns_for_a_non_daemon_caller_when_unwritable(tmp_path):
    result = check_data_dir_writable(
        path=tmp_path, getuid=lambda: _OPERATOR_UID, can_write=_unwritable
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
        result = check_data_dir_writable(path=tmp_path, getuid=lambda: uid)

        assert result.ok is True
        assert result.severity == BLOCK


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores the permission bits this asserts on")
def test_the_default_can_write_seam_tracks_real_permission_bits(tmp_path):
    # Pins the default to os.access, so the fake above cannot drift from what
    # an operator's own uid actually sees on the device.
    root_owned = tmp_path / "visio-recorder"
    root_owned.mkdir(mode=0o500)

    result = check_data_dir_writable(path=root_owned, getuid=lambda: _OPERATOR_UID)

    assert result.severity == WARN
    assert "exists but is not writable" in result.detail


def test_non_daemon_caller_on_a_healthy_device_exits_zero(tmp_path, monkeypatch, capsys):
    """The exact false report: `visio-preflight` as a plain SSH user.

    Every other check passes and the data dir exists, only it is unwritable
    for this uid, and the run must not read as a provisioning failure.
    """
    healthy = [
        CheckResult(name="ffmpeg", severity=BLOCK, ok=True, detail="found on PATH"),
        check_data_dir_writable(
            path=tmp_path, getuid=lambda: _OPERATOR_UID, can_write=_unwritable
        ),
    ]
    monkeypatch.setattr("visio_recorder.preflight.run_checks", lambda source, data_dir: healthy)

    exit_code = main()

    assert has_blocking_failure(healthy) is False
    assert exit_code == 0
    assert "[WARN] data_dir:" in capsys.readouterr().out


def test_daemon_uid_on_the_same_device_still_blocks_startup(tmp_path, monkeypatch):
    # The daemon-side gate is unchanged: the unit's ExecStartPre runs as the
    # daemon's uid and must still refuse to start.
    blocked = [
        check_data_dir_writable(path=tmp_path, getuid=lambda: _DAEMON_UID, can_write=_unwritable)
    ]
    monkeypatch.setattr("visio_recorder.preflight.run_checks", lambda source, data_dir: blocked)

    assert has_blocking_failure(blocked) is True
    assert main() == 1


def test_a_missing_data_dir_blocks_startup_for_a_non_daemon_caller_too(tmp_path, monkeypatch):
    # The regression this pins: an unprovisioned device must not report exit 0
    # just because the operator ran the diagnostic as themselves.
    unprovisioned = [check_data_dir_writable(path=tmp_path / "missing", getuid=lambda: _OPERATOR_UID)]
    monkeypatch.setattr("visio_recorder.preflight.run_checks", lambda source, data_dir: unprovisioned)

    assert has_blocking_failure(unprovisioned) is True
    assert main() == 1


def test_daemon_uid_matches_the_systemd_unit():
    """``_DAEMON_UID`` is only correct while the unit leaves ``User=`` unset.

    Dropping the daemon off root is deliberately deferred to a separate task.
    When it lands, this fails loudly rather than letting the daemon's own
    data-dir gate silently degrade from BLOCK to WARN - which would let it
    start on a device it should have refused, then crash on the first write
    instead of failing legibly at preflight in journalctl.
    """
    user_directives = [
        line.strip()
        for line in _UNIT.read_text().splitlines()
        if line.strip().startswith("User=")
    ]

    assert user_directives == [], (
        f"{_UNIT.name} now sets {user_directives}; update preflight._DAEMON_UID to that "
        "account's uid in the same change, or the daemon's data-dir gate degrades to WARN"
    )
    assert _DAEMON_UID == 0


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
    assert resolve_data_dir({"VISIO_DATA_DIR": "/mnt/usb/visio"}) == Path("/mnt/usb/visio")


def test_resolve_data_dir_matches_daemon_load_config_exactly():
    """Same key, same default, same precedence - including the odd cases.

    ``.get(key, default)`` and ``.get(key) or default`` differ on an empty
    value, and an env file carrying ``VISIO_DATA_DIR=`` gives systemd exactly
    that. Whatever the daemon does with it, the gate has to agree, or it is
    green about a directory the daemon is not using.
    """
    for env in ({}, {"VISIO_DATA_DIR": "/mnt/usb/visio"}, {"VISIO_DATA_DIR": ""}):
        expected = load_config({"SUPABASE_URL": "u", "SUPABASE_ANON_KEY": "k", **env}).data_dir

        assert resolve_data_dir(env) == expected


def test_preflight_and_daemon_share_one_default_data_dir():
    # preflight cannot import daemon (daemon imports preflight), so the two
    # constants are pinned to each other here instead.
    assert _DEFAULT_DATA_DIR == _DAEMON_DEFAULT_DATA_DIR


def test_run_checks_gates_on_the_data_dir_it_is_given(tmp_path):
    configured = tmp_path / "usb-mount"
    configured.mkdir()

    detail = next(r.detail for r in run_checks("none", configured) if r.name == "data_dir")

    assert str(configured) in detail


def test_run_checks_falls_back_to_the_default_data_dir():
    detail = next(r.detail for r in run_checks("none") if r.name == "data_dir")

    assert _DEFAULT_DATA_DIR in detail


def test_main_checks_the_configured_data_dir(monkeypatch, capsys):
    seen = []

    def record(source, data_dir):
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
# --system-site-packages, and rpi_ws281x is setup-device.sh's source build.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        ("rpi_ws281x", "builds it into the venv"),
        ("gpiozero", "python3-gpiozero"),
        ("supabase", "uv sync"),
    ],
)
def test_a_failed_import_names_the_step_that_actually_installs_it(module, expected):
    result = check_importable(module, find_spec=lambda mod: None)

    assert expected in result.detail
    assert result.severity == BLOCK


def test_no_module_is_advised_to_check_a_step_that_does_not_install_it():
    for module in ("rpi_ws281x", "gpiozero"):
        detail = check_importable(module, find_spec=lambda mod: None).detail

        assert "uv sync" not in detail, f"{module} does not come from uv sync"


def test_an_unrecognised_module_still_gets_the_uv_sync_default():
    result = check_importable("some-future-dependency", find_spec=lambda mod: None)

    assert "check the uv sync step" in result.detail
