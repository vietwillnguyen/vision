"""Unit tests for visio_recorder.preflight's individual check functions.

Every check takes an injectable seam (a callable or a Path) defaulting to
the real implementation, so these run without touching real hardware -
the same fake-injection convention used throughout firmware/.
"""

import os
import subprocess

import pytest

from visio_recorder.preflight import (
    BLOCK,
    WARN,
    CheckResult,
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
    run_checks,
)

_DAEMON_UID = 0
_OPERATOR_UID = 1000


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
# The data dir is root-owned on purpose - it holds recorded video - and
# os.access() answers for the caller, so the same failure means different
# things to the daemon and to an operator running visio-preflight over SSH.
# --------------------------------------------------------------------------


def test_check_data_dir_blocks_for_the_daemon_uid(tmp_path):
    result = check_data_dir_writable(path=tmp_path / "missing", getuid=lambda: _DAEMON_UID)

    assert result.ok is False
    assert result.severity == BLOCK
    assert "run setup-device.sh" in result.detail


def test_check_data_dir_only_warns_for_a_non_daemon_caller(tmp_path):
    result = check_data_dir_writable(path=tmp_path / "missing", getuid=lambda: _OPERATOR_UID)

    assert result.ok is False
    assert result.severity == WARN
    # The advice that made the original report misleading: re-running setup
    # would not change anything for this caller.
    assert "setup-device.sh" not in result.detail
    assert str(_OPERATOR_UID) in result.detail


def test_check_data_dir_is_ok_for_either_caller_when_writable(tmp_path):
    for uid in (_DAEMON_UID, _OPERATOR_UID):
        result = check_data_dir_writable(path=tmp_path, getuid=lambda: uid)

        assert result.ok is True
        assert result.severity == BLOCK


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores the permission bits this asserts on")
def test_check_data_dir_warn_detail_distinguishes_unwritable_from_missing(tmp_path):
    root_owned = tmp_path / "visio-recorder"
    root_owned.mkdir(mode=0o500)

    result = check_data_dir_writable(path=root_owned, getuid=lambda: _OPERATOR_UID)

    assert result.severity == WARN
    assert "not writable by" in result.detail
    assert "not a fault" in result.detail


def test_non_daemon_caller_on_a_healthy_device_exits_zero(tmp_path, monkeypatch, capsys):
    """The exact false report: `visio-preflight` as a plain SSH user.

    Every other check passes, only the data dir is unwritable for this uid,
    and the run must not read as a provisioning failure.
    """
    healthy = [
        CheckResult(name="ffmpeg", severity=BLOCK, ok=True, detail="found on PATH"),
        check_data_dir_writable(path=tmp_path / "missing", getuid=lambda: _OPERATOR_UID),
    ]
    monkeypatch.setattr("visio_recorder.preflight.run_checks", lambda source: healthy)

    exit_code = main()

    assert has_blocking_failure(healthy) is False
    assert exit_code == 0
    assert "[WARN] data_dir:" in capsys.readouterr().out


def test_daemon_uid_on_the_same_device_still_blocks_startup(tmp_path, monkeypatch):
    # The daemon-side gate is unchanged: the unit's ExecStartPre runs as the
    # daemon's uid and must still refuse to start.
    blocked = [check_data_dir_writable(path=tmp_path / "missing", getuid=lambda: _DAEMON_UID)]
    monkeypatch.setattr("visio_recorder.preflight.run_checks", lambda source: blocked)

    assert has_blocking_failure(blocked) is True
    assert main() == 1


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
