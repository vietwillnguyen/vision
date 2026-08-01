"""Regression tests for preflight's operator entry point.

These encode the bring-up failure reported from the physical device:

    viet@vision-pendant:~/git/vision/firmware/scripts $ python3 -m visio_recorder.preflight
    /usr/bin/python3: Error while finding module specification for
    'visio_recorder.preflight' (ModuleNotFoundError: No module named 'visio_recorder')

The trigger was a wrong interpreter, but the root cause was that
``firmware/pyproject.toml`` declared no ``[build-system]``. uv then treats the
project as *virtual* (``uv.lock`` records ``source = { virtual = "." }``) and
installs only its dependencies - never ``visio_recorder`` itself. The package
was therefore importable solely via the implicit current-directory entry that
``python -m`` puts on ``sys.path[0]``, so even the documented long invocation
failed from any directory other than ``firmware/``.

The tests below fail if that regresses: they run the interpreter from a
directory that is deliberately not the project root.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from visio_recorder.preflight import (
    BLOCK,
    CheckResult,
    format_report,
    install_root,
    main,
)

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
_SCRIPT_NAME = "visio-preflight"


def _pyproject() -> dict:
    return tomllib.loads(_PYPROJECT.read_text())


def test_pyproject_declares_a_build_system():
    # Without this table uv silently downgrades the project to "virtual" and
    # stops installing it, which is what produced the ModuleNotFoundError.
    assert "build-system" in _pyproject()


def test_pyproject_declares_the_preflight_console_script():
    scripts = _pyproject()["project"]["scripts"]

    assert scripts[_SCRIPT_NAME] == "visio_recorder.preflight:main"


def test_main_is_callable_and_returns_an_int_exit_code(monkeypatch):
    # main() reads only VISIO_BATTERY_SOURCE; "none" drops the PiJuice/I2C
    # checks so this exercises the entry point without battery hardware.
    monkeypatch.setenv("VISIO_BATTERY_SOURCE", "none")

    exit_code = main()

    assert isinstance(exit_code, int)
    assert exit_code in (0, 1)


def test_module_invocation_resolves_from_an_unrelated_directory(tmp_path):
    """The captain's exact failure, reproduced as an assertion."""
    result = subprocess.run(
        [sys.executable, "-m", "visio_recorder.preflight"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "", "VISIO_BATTERY_SOURCE": "none"},
    )

    assert "ModuleNotFoundError" not in result.stderr, result.stderr
    assert "rpicam-vid" in result.stdout


def test_console_script_is_installed_and_runs_from_an_unrelated_directory(tmp_path):
    script = Path(sys.executable).parent / _SCRIPT_NAME
    if not script.exists():
        pytest.skip(f"{script} not present - environment was not built from pyproject.toml")

    result = subprocess.run(
        [str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "", "VISIO_BATTERY_SOURCE": "none"},
    )

    assert "ModuleNotFoundError" not in result.stderr, result.stderr
    assert "rpicam-vid" in result.stdout


def test_install_root_points_at_the_imported_package_directory():
    root = install_root()

    assert root.name == "visio_recorder"
    assert (root / "preflight.py").is_file()


def test_install_root_resolves_the_module_file_it_is_given(tmp_path):
    # Two clones on one device differ only by path, so the reported root has
    # to follow the module actually imported, not a hardcoded constant.
    module_file = tmp_path / "somewhere" / "visio_recorder" / "preflight.py"
    module_file.parent.mkdir(parents=True)
    module_file.touch()

    assert install_root(str(module_file)) == module_file.parent


def test_report_header_names_the_install_the_checks_ran_from():
    results = [CheckResult(name="a", severity=BLOCK, ok=True, detail="fine")]

    report = format_report(results, root=Path("/opt/visio-recorder/firmware/visio_recorder"))

    assert report.splitlines()[0] == "[INFO] install: /opt/visio-recorder/firmware/visio_recorder"
