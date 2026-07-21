"""Unit tests for visio_recorder.preflight's individual check functions.

Every check takes an injectable seam (a callable or a Path) defaulting to
the real implementation, so these run without touching real hardware -
the same fake-injection convention used throughout firmware/.
"""

import subprocess

from visio_recorder.preflight import (
    BLOCK,
    WARN,
    check_binary_on_path,
    check_camera_detected,
    check_data_dir_writable,
    check_i2c_enabled,
    check_importable,
    check_networkmanager_running,
    check_pijuice_importable,
)


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
