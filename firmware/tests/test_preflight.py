"""Unit tests for visio_recorder.preflight's individual check functions.

Every check takes an injectable seam (a callable or a Path) defaulting to
the real implementation, so these run without touching real hardware -
the same fake-injection convention used throughout firmware/.
"""

import os
import subprocess
from pathlib import Path

import pytest

from visio_recorder import daemon
from visio_recorder.daemon import load_config
from visio_recorder.preflight import (
    _DAEMON_UID,
    _DEFAULT_DATA_DIR,
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
    read_env_file,
    resolve_battery_source,
    resolve_data_dir,
    resolve_reported_data_dir,
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
        path=tmp_path / "missing", getuid=lambda: _DAEMON_UID
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
        path=tmp_path / "missing", getuid=lambda: _OPERATOR_UID
    )

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
        result = check_data_dir_writable(path=tmp_path, getuid=_uid(uid))

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

    result = check_data_dir_writable(path=root_owned, getuid=lambda: _OPERATOR_UID)

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
            path=tmp_path, getuid=lambda: _OPERATOR_UID, can_write=_unwritable
        ),
    ]
    monkeypatch.setattr(
        "visio_recorder.preflight.run_checks", lambda source, data_dir, caveat: healthy
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
            path=tmp_path, getuid=lambda: _DAEMON_UID, can_write=_unwritable
        )
    ]
    monkeypatch.setattr(
        "visio_recorder.preflight.run_checks", lambda source, data_dir, caveat: blocked
    )

    assert has_blocking_failure(blocked) is True
    assert main() == 1


def test_a_missing_data_dir_blocks_startup_for_a_non_daemon_caller_too(
    tmp_path, monkeypatch
):
    # The regression this pins: an unprovisioned device must not report exit 0
    # just because the operator ran the diagnostic as themselves.
    unprovisioned = [
        check_data_dir_writable(path=tmp_path / "missing", getuid=lambda: _OPERATOR_UID)
    ]
    monkeypatch.setattr(
        "visio_recorder.preflight.run_checks",
        lambda source, data_dir, caveat: unprovisioned,
    )

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
        f"{_UNIT.name} now sets {user_directives}; update preflight._DAEMON_UID "
        "to that account's uid in the same change, or the daemon's data-dir "
        "gate degrades to WARN"
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

    def record(source, data_dir, caveat):
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

    assert values["VISIO_DATA_DIR"] == "/last/assignment/wins"
    assert values["VISIO_BATTERY_SOURCE"] == "none"
    assert values["SUPABASE_URL"] == "https://x.supabase.co"


def test_read_env_file_strips_one_layer_of_matching_quotes(tmp_path):
    # Quoting is idiomatic in an EnvironmentFile, and a literal directory
    # named '"/mnt/usb"' would be a silent mis-check.
    values = read_env_file(_env_file(tmp_path, 'VISIO_DATA_DIR="/mnt/usb/visio"\n'))

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
        lambda source, data_dir, caveat: [
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


def test_read_env_file_does_not_depend_on_the_ambient_locale(tmp_path, monkeypatch):
    # Under systemd the unit inherits whatever locale it was given, routinely
    # C/POSIX. Decoding a valid UTF-8 path as ASCII would fail on a file that
    # is not corrupt at all.
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("LANG", "C")
    utf8_path = "/mnt/données/visio"

    values = read_env_file(_env_file(tmp_path, f"VISIO_DATA_DIR={utf8_path}\n"))

    assert values["VISIO_DATA_DIR"] == utf8_path
