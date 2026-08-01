"""Tests for the parts of setup-device.sh that can run without a real device.

Most of that script is OS provisioning (apt, raspi-config, systemd) and is only
verifiable on hardware. Two pieces are not, and both are easy to get subtly
wrong, so they are pinned here:

* the ``~/.bashrc`` managed block, whose whole contract is that re-running the
  script is a byte-for-byte no-op rather than an append;
* the ``VISIO_DEV_SHELL`` gate that lets an appliance image opt out.

Rather than restate the shell logic, each test extracts the real functions out
of the script and runs them, so drift between script and test is impossible.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "setup-device.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("awk") is None,
    reason="setup-device.sh's managed-block logic needs bash and awk",
)


def _script_text() -> str:
    return _SCRIPT.read_text()


def _extract_function(name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}$", _script_text(), re.MULTILINE | re.DOTALL)
    assert match, f"{name}() not found in {_SCRIPT} - did it get renamed?"
    return match.group(0)


def _extract_constants() -> str:
    return "\n".join(
        line for line in _script_text().splitlines() if re.match(r"^BASHRC_(BEGIN|END)=", line)
    )


def _extract_bashrc_body() -> str:
    match = re.search(
        r"dev_shell_body=.*?<<'BASHRC_BODY'\n(.*?)\nBASHRC_BODY\n", _script_text(), re.DOTALL
    )
    assert match, "the managed ~/.bashrc body heredoc was not found in setup-device.sh"
    return match.group(1)


def _shell_lib(tmp_path: Path, *functions: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    lib = tmp_path / "extracted.sh"
    lib.write_text(_extract_constants() + "\n" + "\n".join(_extract_function(f) for f in functions) + "\n")
    return lib


def _run(lib: Path, snippet: str, env_file: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail\nsource "{lib}"\n{snippet}'],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "ENV_FILE": str(env_file or "/nonexistent")},
    )


def _write_block(tmp_path: Path, rc: Path, times: int) -> None:
    lib = _shell_lib(tmp_path, "write_managed_block")
    body = tmp_path / "body.txt"
    body.write_text(_extract_bashrc_body())
    snippet = "\n".join(
        [f'body="$(cat "{body}")"'] + [f'write_managed_block "{rc}" "$body" "$(id -un):$(id -gn)"'] * times
    )
    result = _run(lib, snippet)
    assert result.returncode == 0, result.stderr


_EXISTING_BASHRC = """\
# ~/.bashrc
case $- in
    *i*) ;;
      *) return;;
esac
export OPERATOR_SETTING=1
"""


def test_second_run_leaves_bashrc_byte_identical(tmp_path):
    once, twice = tmp_path / "once" / ".bashrc", tmp_path / "twice" / ".bashrc"
    for rc in (once, twice):
        rc.parent.mkdir()
        rc.write_text(_EXISTING_BASHRC)

    _write_block(tmp_path / "a", once, times=1)
    _write_block(tmp_path / "b", twice, times=2)

    assert twice.read_bytes() == once.read_bytes()


def test_repeated_runs_never_duplicate_the_managed_block(tmp_path):
    rc = tmp_path / ".bashrc"
    rc.write_text(_EXISTING_BASHRC)

    _write_block(tmp_path / "w", rc, times=5)

    text = rc.read_text()
    assert text.count("# >>> visio-recorder dev shell") == 1
    assert text.count("# <<< visio-recorder dev shell") == 1


def test_managed_block_preserves_content_outside_it(tmp_path):
    rc = tmp_path / ".bashrc"
    rc.write_text(_EXISTING_BASHRC)

    _write_block(tmp_path / "w", rc, times=3)

    assert "export OPERATOR_SETTING=1" in rc.read_text()


def test_managed_block_is_written_when_bashrc_does_not_exist(tmp_path):
    rc = tmp_path / "fresh" / ".bashrc"
    rc.parent.mkdir()

    _write_block(tmp_path / "w", rc, times=2)

    assert rc.is_file()
    assert rc.read_text().count("# >>> visio-recorder dev shell") == 1


def test_generated_bashrc_is_valid_bash_and_defines_the_conveniences(tmp_path):
    rc = tmp_path / ".bashrc"
    rc.write_text(_EXISTING_BASHRC)
    _write_block(tmp_path / "w", rc, times=1)

    syntax = subprocess.run(["bash", "-n", str(rc)], capture_output=True, text=True, timeout=60)
    assert syntax.returncode == 0, syntax.stderr

    # An interactive shell: the fixture's `case $- in *i*)` guard - the one
    # Raspberry Pi OS ships - returns early otherwise, so a non-interactive
    # source would never reach the managed block at all.
    sourced = subprocess.run(
        ["bash", "--norc", "-i", "-c",
         f'source "{rc}"; alias ll; echo "HC=$HISTCONTROL"; type -t __visio_git_branch'],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "ls -la" in sourced.stdout, sourced.stderr
    assert "HC=ignoreboth:erasedups" in sourced.stdout
    assert "function" in sourced.stdout


@pytest.mark.parametrize("value", ["0", "no", "off", "false", "FALSE", "Off"])
def test_dev_shell_gate_is_disabled_for_falsey_values(tmp_path, value):
    env_file = tmp_path / "visio-recorder.env"
    env_file.write_text(f"SUPABASE_URL=x\nVISIO_DEV_SHELL={value}\n")

    result = _run(_shell_lib(tmp_path, "dev_shell_enabled"), "dev_shell_enabled", env_file)

    assert result.returncode == 1


@pytest.mark.parametrize("value", ["1", "yes", "true", ""])
def test_dev_shell_gate_is_enabled_for_truthy_values(tmp_path, value):
    env_file = tmp_path / "visio-recorder.env"
    env_file.write_text(f"VISIO_DEV_SHELL={value}\n")

    result = _run(_shell_lib(tmp_path, "dev_shell_enabled"), "dev_shell_enabled", env_file)

    assert result.returncode == 0


def test_dev_shell_gate_defaults_to_enabled_when_key_or_file_is_absent(tmp_path):
    lib = _shell_lib(tmp_path, "dev_shell_enabled")
    without_key = tmp_path / "visio-recorder.env"
    without_key.write_text("SUPABASE_URL=x\n")

    assert _run(lib, "dev_shell_enabled", without_key).returncode == 0
    assert _run(lib, "dev_shell_enabled", tmp_path / "missing.env").returncode == 0


def test_env_file_template_documents_the_dev_shell_switch():
    text = _script_text()

    assert "VISIO_DEV_SHELL=1" in text, "the env-file template must ship the key, defaulting to enabled"


def test_dev_shell_targets_the_invoking_user_not_root():
    # The script re-execs itself as root at the top, so $HOME is /root from
    # then on. Every path under the dev-shell step must derive from SUDO_USER.
    step = _script_text().split('log "Step 7/10: developer shell"')[1].split('log "Step 8/10')[0]

    assert 'dev_user="${SUDO_USER:-root}"' in step
    assert 'dev_home="$(getent passwd "${dev_user}"' in step

    # Comments and the two quoted heredocs are operator-facing text evaluated
    # by the operator's own shell, where $HOME and ~ are correct; only the
    # script's own statements are checked.
    body = _extract_bashrc_body()
    code = "\n".join(
        line
        for line in step.replace(body, "").splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "--"))
    )
    assert "HOME" not in code, f"the dev-shell step must not write to root's home:\n{code}"
    assert "~/" not in code, f"tilde would expand to root's home here:\n{code}"


def test_provisioning_pins_an_explicit_revision_instead_of_pulling():
    text = _script_text()

    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r"\bgit\b.*\bpull\b", code), "a bare git pull makes provisioning non-reproducible"
    assert 'checkout --force --detach "${sha}"' in text
    assert "VISIO_GIT_REF" in text


# --------------------------------------------------------------------------
# checkout_revision, exercised against a real local repository. These cover the
# failure modes a bare `git pull` has: a moving branch, a diverged checkout,
# and local edits.
# --------------------------------------------------------------------------

_GIT_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/nonexistent",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}

pytest_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60, env=_GIT_ENV
    )
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def origin(tmp_path):
    """A two-commit upstream repo, plus a tag on the first commit."""
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "firmware.txt").write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "v1")
    first = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "v1.0.0")
    (repo / "firmware.txt").write_text("v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "v2")
    second = _git(repo, "rev-parse", "HEAD")
    return repo, first, second


def _checkout(tmp_path: Path, install: Path, repo: Path, ref: str) -> subprocess.CompletedProcess:
    lib = _shell_lib(tmp_path / "lib", "checkout_revision")
    return subprocess.run(
        ["bash", "-c",
         f'set -euo pipefail\nsource "{lib}"\ncheckout_revision "{install}" "{repo}" "{ref}"'],
        capture_output=True, text=True, timeout=120, env=_GIT_ENV,
    )


@pytest_git
def test_checkout_revision_clones_and_pins_a_tag(tmp_path, origin):
    repo, first, _ = origin
    install = tmp_path / "opt"

    result = _checkout(tmp_path, install, repo, "v1.0.0")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == first
    assert (install / "firmware.txt").read_text() == "v1\n"


@pytest_git
def test_checkout_revision_pins_a_bare_sha(tmp_path, origin):
    repo, first, _ = origin
    install = tmp_path / "opt"

    result = _checkout(tmp_path, install, repo, first)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == first


@pytest_git
def test_checkout_revision_prefers_the_remote_branch_over_a_stale_local_one(tmp_path, origin):
    repo, first, second = origin
    install = tmp_path / "opt"
    _checkout(tmp_path, install, repo, "main")
    # Rewind the local branch: a `git pull` here would merge, and any resolution
    # that consulted the local ref first would provision the wrong commit.
    _git(install, "checkout", "--quiet", "-B", "main", first)

    result = _checkout(tmp_path / "second", install, repo, "main")

    assert result.stdout.strip() == second, result.stderr


@pytest_git
def test_checkout_revision_converges_from_a_diverged_branch_with_local_edits(tmp_path, origin):
    repo, first, second = origin
    install = tmp_path / "opt"
    _checkout(tmp_path, install, repo, "main")
    # Exactly the state a bare `git pull` refuses: a divergent commit plus an
    # uncommitted edit to a tracked file.
    _git(install, "checkout", "--quiet", "-B", "local-drift", first)
    (install / "firmware.txt").write_text("hand-edited on the device\n")
    _git(install, "commit", "--quiet", "-am", "drift")
    (install / "firmware.txt").write_text("and then edited again\n")

    result = _checkout(tmp_path / "second", install, repo, "main")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == second
    assert (install / "firmware.txt").read_text() == "v2\n"


@pytest_git
def test_checkout_revision_preserves_untracked_paths(tmp_path, origin):
    # firmware/.venv lives inside the checkout and must survive an update.
    repo, _, second = origin
    install = tmp_path / "opt"
    _checkout(tmp_path, install, repo, "main")
    venv = install / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "visio-preflight").write_text("#!/bin/sh\n")

    result = _checkout(tmp_path / "second", install, repo, "main")

    assert result.returncode == 0, result.stderr
    assert (venv / "visio-preflight").is_file()


@pytest_git
def test_checkout_revision_fails_on_an_unresolvable_ref(tmp_path, origin):
    repo, _, _ = origin
    install = tmp_path / "opt"

    result = _checkout(tmp_path, install, repo, "no-such-branch")

    assert result.returncode != 0
    assert result.stdout.strip() == ""


@pytest_git
def test_same_ref_on_two_devices_installs_the_same_revision(tmp_path, origin):
    repo, _, second = origin

    a = _checkout(tmp_path / "a", tmp_path / "device-a", repo, "main")
    b = _checkout(tmp_path / "b", tmp_path / "device-b", repo, "main")

    assert a.stdout.strip() == b.stdout.strip() == second


def test_uv_installer_is_version_pinned_and_checksum_verified():
    text = _script_text()

    assert re.search(r'^UV_VERSION="\d+\.\d+\.\d+"$', text, re.MULTILINE)
    assert re.search(r'^UV_INSTALLER_SHA256="[0-9a-f]{64}"$', text, re.MULTILINE)
    assert "sha256sum --check --status" in text
    assert "astral.sh/uv/install.sh" not in text, "the unpinned installer URL must not be piped to sh"
