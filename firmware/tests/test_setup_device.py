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

import os
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


def _write_block(tmp_path: Path, rc: Path, times: int) -> None:
    lib = _shell_lib(tmp_path, "strip_managed_block", "write_managed_block")
    body = tmp_path / "body.txt"
    body.write_text(_extract_bashrc_body())
    snippet = "\n".join(
        [f'body="$(cat "{body}")"'] + [f'write_managed_block "{rc}" "$body" "$(id -un):$(id -gn)"'] * times
    )
    result = _run(lib, snippet)
    assert result.returncode == 0, result.stderr


def _remove_block(tmp_path: Path, rc: Path, times: int = 1) -> None:
    lib = _shell_lib(tmp_path, "strip_managed_block", "remove_managed_block")
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
    lib = _shell_lib(
        tmp_path / "lib", "strip_managed_block", "warn_unterminated_block", "write_managed_block"
    )
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
    lib = _shell_lib(
        tmp_path / "lib", "strip_managed_block", "warn_unterminated_block", "remove_managed_block"
    )
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

    assert result.returncode != 0
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

    assert result.returncode != 0
    assert result.stdout.strip() == "", "a SHA on stdout is read by the caller as a good provision"
    assert second not in result.stdout and third not in result.stdout
    assert "fetching" in result.stderr
    assert (install / "firmware.txt").read_text() == "v2\n", "the checkout is left where it was"


@pytest_git
def test_checkout_revision_fails_loudly_when_the_clone_fails(tmp_path):
    install = tmp_path / "opt"

    result = _checkout(tmp_path, install, tmp_path / "unreachable-origin", "main")

    assert result.returncode != 0
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

    assert result.returncode != 0
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

    assert result.returncode != 0
    assert second not in result.stdout
    assert (install / "firmware.txt").read_text() == "v1\n"


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
