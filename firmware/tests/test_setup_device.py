"""Tests for the parts of setup-device.sh that can run without a real device.

Most of that script is OS provisioning (apt, raspi-config, systemd) and is only
verifiable on hardware. Two pieces are not, and both are easy to get subtly
wrong, so they are pinned here:

* the ``~/.bashrc`` managed block, whose whole contract is that re-running the
  script is a byte-for-byte no-op rather than an append - and that disabling
  the gate removes it again, leaving the rest of the file untouched;
* the ``VISIO_DEV_SHELL`` gate that lets an appliance image opt out, including
  the step ordering that makes it reachable at all.

A third class is pinned here too: the failure signalling of the steps that can
report success while having done nothing. Provisioning that resolves a stale
revision, a gate that reads a quoted value as its opposite, and a strip that
truncates the operator's file are all silent, so each has a test that asserts
the loud outcome rather than the happy path.

Rather than restate the shell logic, each test extracts the real functions out
of the script and runs them, so drift between script and test is impossible.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
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


def _extract_assignment(name: str) -> str:
    match = re.search(rf"^{re.escape(name)}=.*$", _script_text(), re.MULTILINE)
    assert match, f"{name}= not found in {_SCRIPT} - did it get renamed?"
    return match.group(0)


def _marker(name: str) -> str:
    """The literal begin/end line the script writes, as the script defines it."""
    match = re.search(rf'^BASHRC_{name}="(.*)"$', _script_text(), re.MULTILINE)
    assert match, f"BASHRC_{name} not found in {_SCRIPT}"
    return match.group(1)


def _step_block(step: int) -> str:
    """Everything the script does between step ``step`` and the step after it."""
    text = _script_text()
    start = text.index(f'log "Step {step}/10: ')
    return text[start : text.index(f'log "Step {step + 1}/10: ', start)]


def _extract_bashrc_body() -> str:
    match = re.search(
        r"dev_shell_body=.*?<<'BASHRC_BODY'\n(.*?)\nBASHRC_BODY\n", _script_text(), re.DOTALL
    )
    assert match, "the managed ~/.bashrc body heredoc was not found in setup-device.sh"
    return match.group(1)


def _shell_lib(tmp_path: Path, *functions: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    lib = tmp_path / "extracted.sh"
    # log() comes along unconditionally: the error paths under test call it, and
    # a missing log would abort them at 127 before they could report anything.
    lib.write_text(
        _extract_constants()
        + "\n"
        + "\n".join(_extract_function(f) for f in ("log", *functions))
        + "\n"
    )
    return lib


def _run(lib: Path, snippet: str, env_file: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail\nsource "{lib}"\n{snippet}'],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "ENV_FILE": str(env_file or "/nonexistent")},
    )


def _dev_shell_step() -> str:
    return _script_text().split('log "Step 8/10: developer shell"')[1].split('log "Step 9/10')[0]


# Both managed-block entry points reach the operator's file through
# replace_file_via_tmp, so every lib that exercises one carries the whole chain.
_BLOCK_HELPERS = ("strip_managed_block", "warn_unterminated_block", "replace_file_via_tmp")
_WRITE_FUNCTIONS = (*_BLOCK_HELPERS, "write_managed_block")
_REMOVE_FUNCTIONS = (*_BLOCK_HELPERS, "remove_managed_block")


def _write_block(tmp_path: Path, rc: Path, times: int) -> None:
    lib = _shell_lib(tmp_path, *_WRITE_FUNCTIONS)
    body = tmp_path / "body.txt"
    body.write_text(_extract_bashrc_body())
    snippet = "\n".join(
        [f'body="$(cat "{body}")"'] + [f'write_managed_block "{rc}" "$body" "$(id -un):$(id -gn)"'] * times
    )
    result = _run(lib, snippet)
    assert result.returncode == 0, result.stderr


def _remove_block(tmp_path: Path, rc: Path, times: int = 1) -> None:
    lib = _shell_lib(tmp_path, *_REMOVE_FUNCTIONS)
    snippet = "\n".join([f'remove_managed_block "{rc}" "$(id -un):$(id -gn)"'] * times)
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


def test_disabling_the_switch_removes_the_managed_block(tmp_path):
    # Turning VISIO_DEV_SHELL off has to reverse the shell change, not merely
    # stop refreshing it, or an appliance image can never be reached.
    rc = tmp_path / ".bashrc"
    rc.write_text(_EXISTING_BASHRC)
    _write_block(tmp_path / "w", rc, times=1)
    assert "visio-recorder dev shell" in rc.read_text()

    _remove_block(tmp_path / "r", rc)

    assert rc.read_bytes() == _EXISTING_BASHRC.encode()


def test_removing_the_block_is_a_no_op_when_none_is_present(tmp_path):
    rc = tmp_path / ".bashrc"
    rc.write_text(_EXISTING_BASHRC)

    _remove_block(tmp_path / "r", rc, times=3)

    assert rc.read_bytes() == _EXISTING_BASHRC.encode()


def test_removing_the_block_is_a_no_op_when_the_file_does_not_exist(tmp_path):
    rc = tmp_path / "fresh" / ".bashrc"
    rc.parent.mkdir()

    _remove_block(tmp_path / "r", rc)

    assert not rc.exists()


def test_removing_and_rewriting_the_block_round_trips_to_the_same_bytes(tmp_path):
    # Flipping the switch off and back on must land on the enabled fixed
    # point, not accumulate blank lines at the seam.
    once, cycled = tmp_path / "once" / ".bashrc", tmp_path / "cycled" / ".bashrc"
    for rc in (once, cycled):
        rc.parent.mkdir()
        rc.write_text(_EXISTING_BASHRC)
    _write_block(tmp_path / "a", once, times=1)

    _write_block(tmp_path / "b", cycled, times=1)
    _remove_block(tmp_path / "c", cycled)
    _write_block(tmp_path / "d", cycled, times=1)

    assert cycled.read_bytes() == once.read_bytes()


# --------------------------------------------------------------------------
# A begin marker with no end marker. "Skip to EOF" would delete every personal
# setting below it, permanently and silently, on the next run - so both paths
# must refuse the file instead of rewriting it.
# --------------------------------------------------------------------------

_UNTERMINATED_BASHRC = f"""\
# ~/.bashrc
{_marker("BEGIN")}
alias ll='ls -la'
export OPERATOR_SETTING=1
"""


def test_writing_refuses_a_managed_block_with_no_end_marker(tmp_path):
    rc = tmp_path / ".bashrc"
    rc.write_text(_UNTERMINATED_BASHRC)
    lib = _shell_lib(tmp_path / "lib", *_WRITE_FUNCTIONS)
    body = tmp_path / "body.txt"
    body.write_text(_extract_bashrc_body())
    snippet = "\n".join(
        [
            f'body="$(cat "{body}")"',
            f'if write_managed_block "{rc}" "$body" "$(id -un):$(id -gn)"; then',
            "  echo WROTE",
            "else",
            "  echo REFUSED",
            "fi",
        ]
    )

    result = _run(lib, snippet)

    assert result.returncode == 0, result.stderr
    assert "REFUSED" in result.stdout
    assert "no matching end marker" in result.stdout
    assert rc.read_text() == _UNTERMINATED_BASHRC, "the operator's settings must survive"
    assert not (tmp_path / ".bashrc.visio-setup.tmp").exists()


def test_removing_refuses_a_managed_block_with_no_end_marker(tmp_path):
    rc = tmp_path / ".bashrc"
    rc.write_text(_UNTERMINATED_BASHRC)
    lib = _shell_lib(tmp_path / "lib", *_REMOVE_FUNCTIONS)
    snippet = "\n".join(
        [
            f'if remove_managed_block "{rc}" "$(id -un):$(id -gn)"; then',
            "  echo REMOVED",
            "else",
            "  echo REFUSED",
            "fi",
        ]
    )

    result = _run(lib, snippet)

    assert result.returncode == 0, result.stderr
    assert "REFUSED" in result.stdout
    assert rc.read_text() == _UNTERMINATED_BASHRC
    assert not (tmp_path / ".bashrc.visio-setup.tmp").exists()


# --------------------------------------------------------------------------
# A failing write. Both functions are called as `if fn ...; then`, which
# suppresses errexit for their whole body, so nothing but an explicit check
# stands between a half-written temp file and an mv over the operator's
# ~/.bashrc. A full SD card is the realistic cause: step 2 and step 8 both
# apt-install into this filesystem.
# --------------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="a read-only directory does not stop root")
def test_writing_leaves_the_original_intact_when_the_temp_write_fails(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    rc = home / ".bashrc"
    rc.write_text(_EXISTING_BASHRC)
    lib = _shell_lib(tmp_path / "lib", *_WRITE_FUNCTIONS)
    body = tmp_path / "body.txt"
    body.write_text(_extract_bashrc_body())
    snippet = "\n".join(
        [
            f'body="$(cat "{body}")"',
            f'if write_managed_block "{rc}" "$body" "$(id -un):$(id -gn)"; then',
            "  echo WROTE",
            "else",
            "  echo FAILED",
            "fi",
        ]
    )
    home.chmod(0o555)
    try:
        result = _run(lib, snippet)
    finally:
        home.chmod(0o755)

    assert result.returncode == 0, result.stderr
    assert "FAILED" in result.stdout, "an unwritable temp path must not report success"
    assert "is unchanged" in result.stdout
    assert rc.read_bytes() == _EXISTING_BASHRC.encode()
    assert not (home / ".bashrc.visio-setup.tmp").exists(), "no temp litter in the operator's home"


@pytest.mark.skipif(os.geteuid() == 0, reason="a read-only directory does not stop root")
def test_removing_leaves_the_original_intact_when_the_temp_write_fails(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    rc = home / ".bashrc"
    rc.write_text(_EXISTING_BASHRC)
    _write_block(tmp_path / "w", rc, times=1)
    with_block = rc.read_bytes()
    lib = _shell_lib(tmp_path / "lib", *_REMOVE_FUNCTIONS)
    snippet = "\n".join(
        [
            f'if remove_managed_block "{rc}" "$(id -un):$(id -gn)"; then',
            "  echo REMOVED",
            "else",
            "  echo FAILED",
            "fi",
        ]
    )
    home.chmod(0o555)
    try:
        result = _run(lib, snippet)
    finally:
        home.chmod(0o755)

    assert result.returncode == 0, result.stderr
    assert "FAILED" in result.stdout
    assert rc.read_bytes() == with_block
    assert not (home / ".bashrc.visio-setup.tmp").exists()


def test_a_partial_temp_file_is_never_moved_over_the_original(tmp_path):
    # The ENOSPC window the checks above cannot reach: a write that reports
    # success but lands short. Only a temp file whose byte count matches the
    # content may be renamed over the operator's file.
    rc = tmp_path / ".bashrc"
    rc.write_text(_EXISTING_BASHRC)
    lib = _shell_lib(tmp_path / "lib", "replace_file_via_tmp")
    tmp_file = tmp_path / ".bashrc.visio-setup.tmp"
    snippet = "\n".join(
        [
            # A short write that still reports success: full output down a pipe
            # (so the expected length is honest), truncated to a file.
            "printf() {",
            '  if [[ -p /dev/stdout ]]; then command printf "$@"',
            '  else command printf "%s" "${2:0:4}"; fi',
            "}",
            f'if replace_file_via_tmp "{rc}" "{tmp_file}" "$(id -un):$(id -gn)" "a much longer body"; then',
            "  echo REPLACED",
            "else",
            "  echo REFUSED",
            "fi",
        ]
    )

    result = _run(lib, snippet)

    assert result.returncode == 0, result.stderr
    assert "REFUSED" in result.stdout
    assert "refusing to move a partial file" in result.stdout
    assert rc.read_bytes() == _EXISTING_BASHRC.encode()
    assert not tmp_file.exists()


def test_a_refused_bashrc_rewrite_does_not_abort_the_run():
    # Same rule as the unresolvable-user guard: an optional convenience must
    # never cost the data dir and the summary that follow it.
    step = _dev_shell_step()

    assert "if write_managed_block" in step
    assert "if remove_managed_block" in step


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


# The env file is a systemd EnvironmentFile, where quoting the value is both
# valid and idiomatic - and unavoidable once SUPABASE_ANON_KEY is a real JWT.
# A gate that reads VISIO_DEV_SHELL="0" as anything but 0 ships a pendant with
# a dev toolchain on it, silently, which is the outcome the gate exists to
# prevent. So the quoted and space-padded spellings are pinned on both sides.
_GATE = ("env_file_value", "dev_shell_enabled")


@pytest.mark.parametrize(
    "value",
    ["0", "no", "off", "false", "FALSE", "Off", "NO", '"0"', "'0'", '"off"', "0 ", " 0", " false "],
)
def test_dev_shell_gate_is_disabled_for_falsey_values(tmp_path, value):
    env_file = tmp_path / "visio-recorder.env"
    env_file.write_text(f"SUPABASE_URL=x\nVISIO_DEV_SHELL={value}\n")

    result = _run(_shell_lib(tmp_path, *_GATE), "dev_shell_enabled", env_file)

    assert result.returncode == 1


@pytest.mark.parametrize(
    "value", ["1", "yes", "true", "", '"1"', "'yes'", '"true"', "1 ", '""', "TRUE"]
)
def test_dev_shell_gate_is_enabled_for_truthy_values(tmp_path, value):
    env_file = tmp_path / "visio-recorder.env"
    env_file.write_text(f"VISIO_DEV_SHELL={value}\n")

    result = _run(_shell_lib(tmp_path, *_GATE), "dev_shell_enabled", env_file)

    assert result.returncode == 0


def test_dev_shell_gate_defaults_to_enabled_when_key_or_file_is_absent(tmp_path):
    lib = _shell_lib(tmp_path, *_GATE)
    without_key = tmp_path / "visio-recorder.env"
    without_key.write_text("SUPABASE_URL=x\n")

    assert _run(lib, "dev_shell_enabled", without_key).returncode == 0
    assert _run(lib, "dev_shell_enabled", tmp_path / "missing.env").returncode == 0


def test_quoted_but_empty_supabase_values_are_reported_as_missing(tmp_path):
    # Same parser, same class of bug: SUPABASE_ANON_KEY="" is empty as far as
    # the daemon is concerned, so the env-file check must not let it through on
    # the strength of the two quote characters.
    env_file = tmp_path / "visio-recorder.env"
    env_file.write_text('SUPABASE_URL="https://x.supabase.co"\nSUPABASE_ANON_KEY=""\n')

    result = _run(_shell_lib(tmp_path, "env_file_value"), _step_block(7), env_file)

    assert result.returncode == 1
    assert "missing values for: SUPABASE_ANON_KEY" in result.stdout


def test_quoted_supabase_values_satisfy_the_env_file_check(tmp_path):
    env_file = tmp_path / "visio-recorder.env"
    env_file.write_text('SUPABASE_URL="https://x.supabase.co"\nSUPABASE_ANON_KEY="ey.a.jwt"\n')

    result = _run(_shell_lib(tmp_path, "env_file_value"), _step_block(7), env_file)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "already configured" in result.stdout


def test_env_file_template_documents_the_dev_shell_switch():
    text = _script_text()

    assert "VISIO_DEV_SHELL=1" in text, "the env-file template must ship the key, defaulting to enabled"
    template = text.split("""cat > "${ENV_FILE}" <<'EOF'""")[1].split("\nEOF\n")[0]
    # The switch cannot uninstall packages, and pre-seeding is the only route
    # to a device that never carries the toolchain. Both are limitations an
    # operator must read rather than discover.
    assert "does NOT uninstall" in template
    assert "first run" in template


# --------------------------------------------------------------------------
# Step ordering. The gate is only honest if the operator can see the switch
# before anything acts on it: on a fresh device the env-file step writes the
# template and exits, so the developer-shell step first runs on the re-run.
# --------------------------------------------------------------------------


def test_the_env_file_step_runs_before_the_developer_shell_step():
    text = _script_text()

    assert text.index('log "Step 7/10: env file"') < text.index('log "Step 8/10: developer shell"')


def test_step_numbering_is_contiguous_and_matches_the_stated_total():
    numbers = [int(n) for n in re.findall(r'log "Step (\d+)/10: ', _script_text())]

    assert numbers == list(range(1, 11))


def test_the_disabled_branch_removes_the_block_rather_than_only_skipping():
    disabled = _dev_shell_step().split("elif ! dev_shell_enabled; then")[1].split("\nelse\n")[0]

    assert "remove_managed_block" in disabled
    assert "apt-get" not in disabled, "the disabled branch must not install anything"


def test_an_unresolvable_dev_user_skips_the_step_instead_of_aborting(tmp_path):
    # `getent passwd` exits 2 for an unknown account and pipefail propagates
    # that, so an unguarded lookup takes the whole run down at step 8 - losing
    # the data dir and summary over an optional convenience. The guard's own
    # message says "skipping", so it has to skip.
    step = _dev_shell_step()
    lookup = step.split('dev_user="${SUDO_USER:-root}"')[1].split("elif ! dev_shell_enabled")[0]
    snippet = "\n".join(
        [
            'log() { echo "[log] $*"; }',
            'dev_user="definitely-no-such-user"',
            lookup.rstrip(),
            "fi",
            "echo REACHED_THE_NEXT_STEP",
        ]
    )

    result = _run(_shell_lib(tmp_path), snippet)

    assert result.returncode == 0, result.stderr
    assert "skipping the developer shell step" in result.stdout
    assert "REACHED_THE_NEXT_STEP" in result.stdout


def test_dev_shell_targets_the_invoking_user_not_root():
    # The script re-execs itself as root at the top, so $HOME is /root from
    # then on. Every path under the dev-shell step must derive from SUDO_USER.
    step = _dev_shell_step()

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

    assert result.returncode == 1, "status 1 is what makes the caller print the ref advice"
    assert result.stdout.strip() == ""
    # The two failures need different operator advice - push your branch versus
    # fix your network - so they must not report each other.
    assert "fetching" not in result.stderr


@pytest_git
def test_checkout_revision_fails_loudly_when_the_fetch_fails(tmp_path, origin):
    # The case that otherwise reports success on stale firmware. The install dir
    # already carries a remote-tracking origin/main from an earlier run, so an
    # unchecked fetch failure falls straight through to rev-parse, resolves that
    # OLD commit, prints it and returns 0 - and the script logs "Provisioned
    # revision <old sha>" over a device that never received the new firmware.
    repo, _, second = origin
    install = tmp_path / "opt"
    _checkout(tmp_path, install, repo, "main")
    assert _git(install, "rev-parse", "refs/remotes/origin/main") == second
    (repo / "firmware.txt").write_text("v3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "v3")
    third = _git(repo, "rev-parse", "HEAD")

    result = _checkout(tmp_path / "second", install, tmp_path / "unreachable-origin", "main")

    assert result.returncode == 2, "git failures report themselves and must not draw ref advice"
    assert result.stdout.strip() == "", "a SHA on stdout is read by the caller as a good provision"
    assert second not in result.stdout and third not in result.stdout
    assert "fetching" in result.stderr
    assert (install / "firmware.txt").read_text() == "v2\n", "the checkout is left where it was"


@pytest_git
def test_checkout_revision_fails_loudly_when_the_clone_fails(tmp_path):
    install = tmp_path / "opt"

    result = _checkout(tmp_path, install, tmp_path / "unreachable-origin", "main")

    assert result.returncode == 2
    assert result.stdout.strip() == ""
    assert "cloning" in result.stderr


@pytest_git
def test_checkout_revision_fails_loudly_when_the_install_dir_is_not_a_healthy_repo(tmp_path, origin):
    # A .git that is not a repository takes the update path rather than the
    # clone path, so the first git command to run is `remote set-url`.
    repo, _, _ = origin
    install = tmp_path / "opt"
    (install / ".git").mkdir(parents=True)

    result = _checkout(tmp_path, install, repo, "main")

    assert result.returncode == 2
    assert result.stdout.strip() == ""
    assert "origin remote" in result.stderr


@pytest_git
@pytest.mark.skipif(os.geteuid() == 0, reason="a read-only directory does not stop root")
def test_checkout_revision_fails_loudly_when_the_working_tree_cannot_be_written(tmp_path, origin):
    # Stands in for the disk-full checkout: fetch succeeds and the ref resolves,
    # so an unchecked `git checkout` leaves the tree on the old revision while
    # the function prints the new SHA - and uv sync then installs from it.
    repo, first, second = origin
    install = tmp_path / "opt"
    _checkout(tmp_path, install, repo, first)
    install.chmod(0o555)
    try:
        result = _checkout(tmp_path / "second", install, repo, "main")
    finally:
        install.chmod(0o755)

    assert result.returncode == 2
    assert second not in result.stdout
    assert (install / "firmware.txt").read_text() == "v1\n"


# --------------------------------------------------------------------------
# The caller's advice. checkout_revision fails for five distinct reasons; only
# one of them is "that ref does not exist". The last line on the operator's
# screen must not contradict the three lines above it.
# --------------------------------------------------------------------------


def _checkout_caller() -> str:
    text = _script_text()
    start = text.index("checkout_status=0")
    return text[start : text.index('log "Provisioned revision', start)]


def _run_caller(tmp_path: Path, status: int) -> subprocess.CompletedProcess:
    snippet = "\n".join(
        [
            _extract_function("log"),
            'INSTALL_DIR="/opt/visio-recorder"',
            'REPO_URL="https://example.invalid/vision.git"',
            'requested_ref="some-ref"',
            f"checkout_revision() {{ return {status}; }}",
            _checkout_caller(),
        ]
    )
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{snippet}"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin"},
    )


def test_the_caller_gives_ref_advice_only_when_the_ref_is_the_problem(tmp_path):
    unresolvable = _run_caller(tmp_path, 1)

    assert unresolvable.returncode == 1
    assert "cannot resolve 'some-ref'" in unresolvable.stdout
    assert "push it first" in unresolvable.stdout


def test_the_caller_stays_silent_when_the_function_already_explained(tmp_path):
    git_failure = _run_caller(tmp_path, 2)

    assert git_failure.returncode == 1, "a reported git failure is still fatal"
    assert "push it first" not in git_failure.stdout, "this contradicts the real cause"
    assert "cannot resolve" not in git_failure.stdout


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


def test_the_pinned_digest_matches_what_astral_actually_serves():
    # A digest that is merely well-formed proves nothing: a typo, a bad
    # copy-paste, or a version bump that moved UV_VERSION without moving
    # UV_INSTALLER_SHA256 all leave the pin syntactically perfect and make
    # every device's step 4 abort at the checksum. Fetch the real artifact and
    # compare. A mismatch here is also the signal that upstream re-published
    # that URL's contents, which is exactly what pinning exists to catch.
    version = re.search(r'^UV_VERSION="([^"]+)"$', _script_text(), re.MULTILINE).group(1)
    expected = re.search(r'^UV_INSTALLER_SHA256="([^"]+)"$', _script_text(), re.MULTILINE).group(1)
    url = f"https://astral.sh/uv/{version}/install.sh"
    # astral.sh answers urllib's default User-Agent with 403, so send the one
    # the script's own curl invocation would - otherwise this test looks like a
    # network outage and skips itself on every run.
    request = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest.skip(f"cannot reach {url} ({exc}) - the pin was not verified against upstream")

    assert hashlib.sha256(payload).hexdigest() == expected, (
        f"{url} no longer hashes to the digest pinned in setup-device.sh. Either the "
        f"pin is wrong and every device will abort at step 4, or upstream changed the "
        f"artifact - re-record it deliberately, do not paste the new value in blind."
    )


def test_uv_installs_to_a_directory_later_runs_can_actually_see():
    # The installer's default is ${HOME}/.local/bin, which is root's home after
    # the re-exec and is on no later run's PATH, because sudo hands the script
    # `secure_path`. Installing there makes every re-run download again.
    text = _script_text()

    assert 'UV_INSTALL_DIR="${BIN_DIR}"' in text
    assert 'BIN_DIR="/usr/local/bin"' in text, "the install dir has to be on sudo's secure_path"
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "${HOME}/.local/bin" not in code


def test_uv_step_reuses_an_existing_install_instead_of_downloading_again(tmp_path):
    # An operator re-runs this on a device with no working internet to repair
    # drift - the one scenario the rest of the script tolerates, since the git
    # checkout converges from cache. A curl that is never called is the point.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    installed = bin_dir / "uv"
    installed.write_text("#!/bin/sh\necho 'uv 0.0.0-stub'\n")
    installed.chmod(0o755)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    curl = stubs / "curl"
    curl.write_text("#!/bin/sh\necho CURL_WAS_CALLED >&2\nexit 1\n")
    curl.chmod(0o755)

    snippet = "\n".join(
        [
            _extract_function("log"),
            _extract_assignment("UV_VERSION"),
            _extract_assignment("UV_INSTALLER_SHA256"),
            f'BIN_DIR="{bin_dir}"',
            # PATH deliberately excludes ${BIN_DIR}, the way sudo's secure_path
            # excludes wherever the installer's default would have put it.
            _step_block(4),
            "uv --version",
        ]
    )
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{snippet}"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(tmp_path / "home")},
    )

    assert result.returncode == 0, result.stderr
    assert "CURL_WAS_CALLED" not in result.stderr, "an installed uv must not be downloaded again"
    assert "reusing it, nothing downloaded" in result.stdout
    assert "uv 0.0.0-stub" in result.stdout, "the reused uv has to end up on PATH"


# --------------------------------------------------------------------------
# Step 1's swap sizing. `set -euo pipefail` is on from line 2, so every value
# CONF_SWAPSIZE can legitimately hold has to survive an arithmetic comparison:
# "auto", "1024M", a trailing comment, or the key being absent entirely. Any of
# those aborting takes the whole provision down at the first step, before a
# single package is installed - and dphys-swapfile itself accepts them.
# --------------------------------------------------------------------------


def _run_swap_step(tmp_path: Path, conf_text: str | None) -> subprocess.CompletedProcess:
    """Run the real step 1, with only its three path constants redirected."""
    conf = tmp_path / "dphys-swapfile"
    if conf_text is not None:
        conf.write_text(conf_text)
    block = _step_block(1)
    for name, value in (
        ("SWAP_CONF", str(conf)),
        # Absent on purpose: the dphys-swapfile branch is the one under test,
        # and it is checked first, so these must not exist.
        ("RPI_SWAP_CONF", str(tmp_path / "no-rpi-swap.conf")),
        ("RPI_SWAP_DROPIN", str(tmp_path / "no-rpi-swap.conf.d" / "10-visio.conf")),
    ):
        block, count = re.subn(rf'^{name}="[^"]*"$', f'{name}="{value}"', block, flags=re.MULTILINE)
        assert count == 1, f"{name} is no longer a single quoted assignment in step 1"

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    systemctl = stubs / "systemctl"
    systemctl.write_text('#!/bin/sh\necho "SYSTEMCTL $*"\n')
    systemctl.chmod(0o755)

    snippet = "\n".join([_extract_function("log"), "reboot_needed=false", block])
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{snippet}"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": f"{stubs}:/usr/bin:/bin"},
    )


@pytest.mark.parametrize(
    "value",
    [
        "auto",  # dphys-swapfile's own "size it from RAM" setting
        "1024M",  # a unit suffix, which the file's own comments use
        "512 # bumped by hand during bring-up",
        '"1024"',  # quoted, the way the env file's keys are
        "",  # CONF_SWAPSIZE= with nothing after it
    ],
)
def test_a_non_numeric_swap_size_does_not_abort_the_run(tmp_path, value):
    result = _run_swap_step(tmp_path, f"CONF_SWAPSIZE={value}\n")

    assert result.returncode == 0, f"step 1 aborted on CONF_SWAPSIZE={value!r}: {result.stderr}"
    assert "not a plain integer" in result.stdout
    # Treated as 0, so the script goes on to write the size it guarantees.
    assert (tmp_path / "dphys-swapfile").read_text() == "CONF_SWAPSIZE=1024\n"


def test_a_missing_swap_size_key_does_not_abort_the_run(tmp_path):
    # grep matches nothing, and pipefail makes that a failure without the
    # script's `|| true`; the empty string then reaches the comparison.
    result = _run_swap_step(tmp_path, "# CONF_SWAPSIZE is commented out\n")

    assert result.returncode == 0, result.stderr
    assert "missing or not a plain integer" in result.stdout


def test_a_swap_size_below_the_floor_is_raised_without_complaint(tmp_path):
    result = _run_swap_step(tmp_path, "CONF_SWAPSIZE=100\nCONF_SWAPFILE=/var/swap\n")

    assert result.returncode == 0, result.stderr
    assert "not a plain integer" not in result.stdout, "100 is a plain integer"
    assert "increased to 1024MB" in result.stdout
    assert (tmp_path / "dphys-swapfile").read_text() == "CONF_SWAPSIZE=1024\nCONF_SWAPFILE=/var/swap\n"


def test_a_sufficient_swap_size_is_left_alone(tmp_path):
    result = _run_swap_step(tmp_path, "CONF_SWAPSIZE=2048\n")

    assert result.returncode == 0, result.stderr
    assert "no change needed" in result.stdout
    assert "SYSTEMCTL" not in result.stdout, "nothing to restart when nothing changed"
    assert (tmp_path / "dphys-swapfile").read_text() == "CONF_SWAPSIZE=2048\n"


# --------------------------------------------------------------------------
# Step 5's venv. ${PREFLIGHT_BIN} and the systemd unit's ExecStart/ExecStartPre
# all run firmware/.venv/bin/python3, and the daemon's real drivers import
# gpiozero, pijuice and rpi_ws281x - none of which are in
# firmware/pyproject.toml, because they do not build off a Pi. `uv sync` alone
# builds a venv that can import none of them: it is isolated from the system
# site-packages, and uv resolves an interpreter it downloaded for itself rather
# than /usr/bin/python3, whose dist-packages are where apt puts them. The
# result is a fully provisioned device on which preflight can never exit 0, so
# ExecStartPre never lets the daemon start.
# --------------------------------------------------------------------------


def _system_python() -> str:
    match = re.search(r'^SYSTEM_PYTHON="([^"]+)"$', _script_text(), re.MULTILINE)
    assert match, "SYSTEM_PYTHON= not found in setup-device.sh - did it get renamed?"
    return match.group(1)


_VENV_FUNCTIONS = ("venv_sees_system_packages", "ensure_system_site_venv")

pytest_uv = pytest.mark.skipif(
    shutil.which("uv") is None or not Path(_system_python()).exists(),
    reason=f"needs uv and {_system_python()} to build a real venv",
)


def _venv_lib(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    lib = tmp_path / "venv.sh"
    lib.write_text(
        "\n".join(
            [
                _extract_function("log"),
                _extract_assignment("SYSTEM_PYTHON"),
                *(_extract_function(f) for f in _VENV_FUNCTIONS),
            ]
        )
        + "\n"
    )
    return lib


def _run_venv(lib: Path, snippet: str, home: Path, path: str = "") -> subprocess.CompletedProcess:
    uv_dir = os.path.dirname(shutil.which("uv") or "/nonexistent")
    home.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail\nsource "{lib}"\n{snippet}'],
        capture_output=True,
        text=True,
        timeout=300,
        env={"PATH": f"{path}{uv_dir}:/usr/bin:/bin", "HOME": str(home)},
    )


def _sys_path(python: Path | str) -> list[str]:
    result = subprocess.run(
        [str(python), "-c", "import json, sys; print(json.dumps(sys.path))"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest_uv
def test_the_provisioned_venv_can_import_the_apt_installed_hardware_libraries(tmp_path):
    venv = tmp_path / "firmware" / ".venv"

    result = _run_venv(_venv_lib(tmp_path / "lib"), f'ensure_system_site_venv "{venv}"', tmp_path / "home")

    assert result.returncode == 0, result.stderr
    cfg = (venv / "pyvenv.cfg").read_text()
    assert "include-system-site-packages = true" in cfg, cfg
    system_sites = subprocess.run(
        [_system_python(), "-c", "import json, site; print(json.dumps(site.getsitepackages()))"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert system_sites.returncode == 0, system_sites.stderr
    shared = set(json.loads(system_sites.stdout)) & set(_sys_path(venv / "bin" / "python3"))
    assert shared, (
        "the provisioned venv cannot see a single system site-packages directory, so "
        "gpiozero and pijuice - apt packages on the device - are unimportable from it "
        f"and preflight blocks on them. venv sys.path: {_sys_path(venv / 'bin' / 'python3')}"
    )


@pytest_uv
def test_a_healthy_venv_is_reused_rather_than_rebuilt_on_a_re_run(tmp_path):
    # Rebuilding on every pass would throw away the compiled rpi_ws281x below
    # it and recompile it, which is minutes on a Pi Zero 2W.
    venv = tmp_path / ".venv"
    lib, home = _venv_lib(tmp_path / "lib"), tmp_path / "home"
    assert _run_venv(lib, f'ensure_system_site_venv "{venv}"', home).returncode == 0
    marker = venv / "installed-by-a-previous-run"
    marker.write_text("x")

    result = _run_venv(lib, f'ensure_system_site_venv "{venv}"', home)

    assert result.returncode == 0, result.stderr
    assert marker.exists(), "a venv that already sees the system packages must be left alone"
    assert "recreating it" not in result.stdout


def test_a_venv_uv_built_on_its_own_is_recreated_from_the_system_interpreter(tmp_path):
    # Exactly what `uv sync` leaves behind with no venv present: isolated, and
    # built from an interpreter uv downloaded for itself, whose site-packages
    # hold nothing apt ever installed. Flipping the flag in pyvenv.cfg would
    # not help - the base interpreter is fixed at creation - so it is rebuilt.
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(
        "home = /root/.local/share/uv/python/cpython-3.13.1-linux-aarch64-gnu/bin\n"
        "implementation = CPython\n"
        "version_info = 3.13.1\n"
        "include-system-site-packages = false\n"
    )
    stale = venv / "left-by-the-isolated-venv"
    stale.write_text("x")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    uv = stubs / "uv"
    uv.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" > "{tmp_path}/uv-argv"\n')
    uv.chmod(0o755)

    result = _run_venv(
        _venv_lib(tmp_path / "lib"),
        f'ensure_system_site_venv "{venv}"',
        tmp_path / "home",
        path=f"{stubs}:",
    )

    assert result.returncode == 0, result.stderr
    assert "cannot see the system site-packages" in result.stdout
    assert not stale.exists(), "the isolated venv has to be replaced, not added to"
    argv = (tmp_path / "uv-argv").read_text()
    assert "--system-site-packages" in argv, argv
    assert f"--python {_system_python()}" in argv, argv


def test_step_5_builds_the_venv_before_syncing_and_rechecks_it_afterwards():
    step = _step_block(5)

    assert step.index('ensure_system_site_venv "${VENV_DIR}"') < step.index("uv sync --locked")
    # A venv that lost system-site-packages during the sync is the exact
    # failure this step exists to prevent, and it is invisible until the daemon
    # will not start, so the run refuses to report success on one.
    assert "if ! venv_sees_system_packages" in step
    # The compiled hardware wheel is not in uv.lock, so an exact sync deletes
    # it on every re-run and rebuilds it from source.
    assert "uv sync --locked --inexact" in step


def test_the_hardware_libraries_preflight_blocks_on_are_actually_provisioned():
    # preflight blocks on all three (see visio_recorder/preflight.py's
    # run_checks), so provisioning has to supply all three or no device it
    # produces can pass its own preflight.
    text = _script_text()

    assert "python3-gpiozero" in _step_block(2), "gpiozero is an apt package on the target"
    assert "pijuice-base" in _step_block(2)
    assert re.search(r'^RPI_WS281X_SPEC="rpi-ws281x==\d+\.\d+\.\d+"$', text, re.MULTILINE), (
        "no archive ships rpi_ws281x, so it is built into the venv - pinned, like the uv installer"
    )
    assert 'uv pip install --python "${VENV_DIR}/bin/python3" "${RPI_WS281X_SPEC}"' in _step_block(5)


def _ws281x_block() -> str:
    step = _step_block(5)
    start = step.index("if \"${VENV_DIR}/bin/python3\" -c 'import rpi_ws281x'")
    return step[start : step.index("# Put preflight on PATH", start)]


def test_a_failed_rpi_ws281x_build_does_not_abort_provisioning(tmp_path):
    # A device with no compiler or no network still needs its systemd unit, its
    # env file and its data dir; preflight reports the missing module itself.
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in (
        # No candidate for anything, so the build-dependency install is skipped.
        ("apt-cache", "#!/bin/sh\nexit 0\n"),
        ("apt-get", '#!/bin/sh\necho "APT_GET_WAS_CALLED $*" >&2\nexit 100\n'),
        ("uv", "#!/bin/sh\necho 'error: failed to build rpi-ws281x' >&2\nexit 1\n"),
    ):
        stub = stubs / name
        stub.write_text(body)
        stub.chmod(0o755)
    lib = tmp_path / "lib.sh"
    lib.write_text(
        "\n".join(
            _extract_function(f) for f in ("log", "apt_candidate", "apt_has_candidate")
        )
        + "\n"
    )
    snippet = "\n".join(
        [
            _extract_assignment("PREFLIGHT_BIN"),
            _extract_assignment("RPI_WS281X_SPEC"),
            f'VENV_DIR="{tmp_path}/no-such-venv"',
            _ws281x_block().rstrip(),
            "echo REACHED_THE_NEXT_STEP",
        ]
    )
    result = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\nsource "{lib}"\n{snippet}'],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": f"{stubs}:/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    assert "APT_GET_WAS_CALLED" not in result.stderr, "nothing to install when there is no candidate"
    assert "could not install rpi-ws281x" in result.stdout
    assert "blocking failure" in result.stdout
    assert "REACHED_THE_NEXT_STEP" in result.stdout


# --------------------------------------------------------------------------
# Step 8's remaining unguarded writes. The step promises twice in its own
# comments that optional convenience work is skippable - an account with no
# passwd entry, a ~/.bashrc it refuses to parse - and honours that for both via
# `if remove_managed_block` / `if write_managed_block`. Under `set -e` the
# neovim config write and the dev-package install had no such guard, so a home
# on a full or read-only filesystem, a regular file where ~/.config should be,
# or a transient apt mirror error took the whole run down at step 8 - before
# step 9 creates the data dir, which preflight then blocks on.
# --------------------------------------------------------------------------


def _nvim_block() -> str:
    step = _dev_shell_step()
    start = step.index('  nvim_starter_config="$(cat ')
    return step[start : step.index("  # Quoted heredoc:", start)]


def _run_nvim_block(tmp_path: Path, home: Path) -> subprocess.CompletedProcess:
    lib = _shell_lib(tmp_path / "lib", *_BLOCK_HELPERS)
    snippet = "\n".join(
        [
            f'dev_home="{home}"',
            'dev_user="$(id -un)"',
            'dev_group="$(id -gn)"',
            _nvim_block().rstrip(),
            "echo REACHED_THE_NEXT_STEP",
        ]
    )
    return _run(lib, snippet)


def test_a_failed_nvim_config_write_does_not_abort_the_run(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    # A regular file where the directory belongs: mkdir -p fails with EEXIST.
    (home / ".config").write_text("not a directory\n")

    result = _run_nvim_block(tmp_path, home)

    assert result.returncode == 0, result.stderr
    assert "skipping the starter neovim config" in result.stdout
    assert "REACHED_THE_NEXT_STEP" in result.stdout


def test_the_starter_nvim_config_is_written_once_and_then_left_alone(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    first = _run_nvim_block(tmp_path / "first", home)

    init = home / ".config" / "nvim" / "init.lua"
    assert first.returncode == 0, first.stderr
    assert "Wrote starter" in first.stdout
    assert init.read_text().startswith("-- Starting point written once by setup-device.sh.")
    assert init.read_text().endswith("\n")
    assert not (home / ".config" / "nvim" / "init.lua.visio-setup.tmp").exists()

    init.write_text("-- edited by the operator\n")
    second = _run_nvim_block(tmp_path / "second", home)

    assert second.returncode == 0, second.stderr
    assert "left untouched" in second.stdout
    assert init.read_text() == "-- edited by the operator\n"


def test_a_failed_dev_package_install_does_not_abort_the_run(tmp_path):
    step = _dev_shell_step()
    block = step[step.index("  dev_packages=(") : step.index("  # System-wide default")]
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in (
        ("apt-cache", '#!/bin/sh\necho "  Candidate: 1.0"\n'),
        ("apt-get", "#!/bin/sh\nexit 100\n"),
    ):
        stub = stubs / name
        stub.write_text(body)
        stub.chmod(0o755)
    lib = tmp_path / "lib.sh"
    lib.write_text(
        "\n".join(
            _extract_function(f) for f in ("log", "apt_candidate", "apt_has_candidate")
        )
        + "\n"
    )
    snippet = "\n".join([block.rstrip(), "echo REACHED_THE_NEXT_STEP"])
    result = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\nsource "{lib}"\n{snippet}'],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": f"{stubs}:/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    assert "continuing without them" in result.stdout
    assert "REACHED_THE_NEXT_STEP" in result.stdout


# --------------------------------------------------------------------------
# Steps 9 and 10. A non-fatal step-5 failure that leaves the daemon unable to
# start has to reach the summary: on a Pi Zero 2W thousands of lines of apt
# output scroll between the two, so a mid-run warning is realistically gone by
# the time the operator reads "Setup complete". And the directory step 9
# creates must be the one preflight checks and the daemon records into, or
# preflight blocks on a path nothing ever creates and advises a re-run that
# would not create it either.
# --------------------------------------------------------------------------


def _run_final_steps(
    tmp_path: Path, env_text: str, ws281x_missing: str = "false"
) -> subprocess.CompletedProcess:
    """Run steps 9 and 10 for real, with only DATA_DIR redirected."""
    env_file = tmp_path / "visio-recorder.env"
    env_file.write_text(env_text)
    block = 'log "Step 9/10: data dir"' + _script_text().split('log "Step 9/10: data dir"')[1]
    snippet = "\n".join(
        [
            _extract_function("log"),
            _extract_function("env_file_value"),
            _extract_assignment("PREFLIGHT_BIN"),
            _extract_assignment("RPI_WS281X_SPEC"),
            f'DATA_DIR="{tmp_path}/default-data-dir"',
            'target_sha="deadbee"',
            "reboot_needed=false",
            "data_dir_failed=false",
            f"ws281x_missing={ws281x_missing}",
            block,
        ]
    )
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{snippet}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={"PATH": "/usr/bin:/bin", "ENV_FILE": str(env_file)},
    )


_CONFIGURED_ENV = "SUPABASE_URL=u\nSUPABASE_ANON_KEY=k\nVISIO_BATTERY_SOURCE=none\n"


def test_a_failed_ws281x_build_is_repeated_in_the_summary(tmp_path):
    result = _run_final_steps(tmp_path, _CONFIGURED_ENV, ws281x_missing="true")

    assert result.returncode == 0, result.stderr
    assert "INCOMPLETE" in result.stdout
    assert "rpi_ws281x" in result.stdout
    assert "daemon will not start" in result.stdout
    # Still non-fatal: the data dir and the rest of the summary happen anyway.
    assert (tmp_path / "default-data-dir").is_dir()
    assert "Setup complete." in result.stdout


def test_a_successful_run_says_nothing_about_ws281x(tmp_path):
    result = _run_final_steps(tmp_path, _CONFIGURED_ENV, ws281x_missing="false")

    assert result.returncode == 0, result.stderr
    assert "INCOMPLETE" not in result.stdout
    assert "Setup complete." in result.stdout


def test_the_data_dir_step_provisions_the_configured_directory(tmp_path):
    configured = tmp_path / "usb-mount" / "visio"
    result = _run_final_steps(
        tmp_path, _CONFIGURED_ENV + f"VISIO_DATA_DIR={configured}\n", ws281x_missing="false"
    )

    assert result.returncode == 0, result.stderr
    assert configured.is_dir(), "preflight checks this path, so provisioning has to create it"
    assert not (tmp_path / "default-data-dir").exists()
    assert str(configured) in result.stdout, "the summary has to name the directory it made"


def test_the_data_dir_step_falls_back_to_the_default_when_unset(tmp_path):
    result = _run_final_steps(tmp_path, _CONFIGURED_ENV, ws281x_missing="false")

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "default-data-dir").is_dir()


def test_a_quoted_data_dir_is_unquoted_the_way_systemd_reads_it(tmp_path):
    # The env file is a systemd EnvironmentFile, where quoting is idiomatic;
    # a literal directory named '"/mnt/usb"' would be a silent mis-provision.
    configured = tmp_path / "quoted-mount"
    result = _run_final_steps(
        tmp_path, _CONFIGURED_ENV + f'VISIO_DATA_DIR="{configured}"\n', ws281x_missing="false"
    )

    assert result.returncode == 0, result.stderr
    assert configured.is_dir()


# --------------------------------------------------------------------------
# Step 9's argument is operator input now, not a constant the script owns.
# `mkdir -p /var/lib/visio-recorder` as root is effectively infallible; an
# env-file value is not, and dying here would take step 10 with it - the very
# report that tells the operator what went wrong.
# --------------------------------------------------------------------------


def test_an_uncreatable_data_dir_is_reported_in_the_summary_instead_of_aborting(tmp_path):
    blocked = tmp_path / "occupied"
    blocked.write_text("a regular file where the data dir should be\n")

    result = _run_final_steps(tmp_path, _CONFIGURED_ENV + f"VISIO_DATA_DIR={blocked}/visio\n")

    assert result.returncode == 0, result.stderr
    assert "cannot create" in result.stdout
    assert "VISIO_DATA_DIR" in result.stdout
    assert "INCOMPLETE" in result.stdout
    assert "was NOT created" in result.stdout
    # The report the abort would have swallowed.
    assert "Provisioned revision: deadbee" in result.stdout
    assert "Setup complete." in result.stdout


def test_a_relative_data_dir_is_rejected_rather_than_silently_diverging(tmp_path):
    # It would resolve against this script's cwd here but against the unit's
    # WorkingDirectory= for the daemon, so the two would record to different
    # places without either one reporting a problem.
    result = _run_final_steps(tmp_path, _CONFIGURED_ENV + "VISIO_DATA_DIR=recordings\n")

    assert result.returncode == 0, result.stderr
    assert "not an absolute path" in result.stdout
    assert "INCOMPLETE" in result.stdout
    assert not (tmp_path / "recordings").exists()
    assert not (tmp_path / "default-data-dir").exists(), "a bad value must not fall back silently"
    assert "Setup complete." in result.stdout


def test_a_created_data_dir_says_nothing_about_being_incomplete(tmp_path):
    result = _run_final_steps(tmp_path, _CONFIGURED_ENV)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "default-data-dir").is_dir()
    assert "INCOMPLETE" not in result.stdout


def test_the_env_template_does_not_promise_preflight_can_read_it(tmp_path):
    # The file is chmod 600 root-owned, so the unprivileged read fails; the
    # template must not document that path as working.
    template = _script_text().split("""cat > "${ENV_FILE}" <<'EOF'""")[1].split("\nEOF\n")[0]

    assert "VISIO_DATA_DIR" in template
    assert template.count("VISIO_DATA_DIR=") == 1, "the key must not be duplicated"
    assert "600" in template or "root-owned" in template
    assert "sudo" in template
