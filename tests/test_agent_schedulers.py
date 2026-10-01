"""The agent users are denied cron and at (Claude Security finding
4773340, criterion 1; design/untrusted-agent-job.md → What stays in the
untrusted job): `create-codex-user/deny-schedulers.sh` appends the user
to `cron.deny` and `at.deny` (created when missing, once, on its own
line), an allow file that lists the user fails the step, and the create
mode runs it right after the user exists.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_codex_path import composite_runs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ROOT / ".github" / "actions"
DENY = ACTIONS / "create-codex-user" / "deny-schedulers.sh"
CREATE = ACTIONS / "create-codex-user" / "action.yml"


# --- deny-schedulers.sh ---------------------------------------------------------


def deny(etc: Path, user: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(DENY), user, str(etc)], capture_output=True, text=True, check=False)


@pytest.mark.parametrize("user", ["codex", "claude-agent"])
def test_deny_creates_both_files_with_the_user(tmp_path, user):
    r = deny(tmp_path, user)
    assert r.returncode == 0, r.stdout + r.stderr
    for tool in ("cron", "at"):
        f = tmp_path / f"{tool}.deny"
        assert f.read_text() == f"{user}\n"
        assert stat.S_IMODE(f.stat().st_mode) == 0o644
        assert f"{tool} denied to {user}" in r.stdout


def test_deny_appends_once_on_its_own_line(tmp_path):
    (tmp_path / "at.deny").write_text("daemon\nbin")  # the package's file, last newline missing
    (tmp_path / "cron.deny").write_text("nobody\n")
    for _ in range(2):
        r = deny(tmp_path, "codex")
        assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "at.deny").read_text() == "daemon\nbin\ncodex\n"
    assert (tmp_path / "cron.deny").read_text() == "nobody\ncodex\n"
    r = deny(tmp_path, "claude-agent")
    assert (tmp_path / "cron.deny").read_text() == "nobody\ncodex\nclaude-agent\n"


@pytest.mark.parametrize("tool", ["cron", "at"])
def test_an_allow_file_that_lists_the_user_fails(tmp_path, tool):
    # With an allow file, the tools read only it: a listed user would be
    # allowed whatever the deny file says.
    (tmp_path / f"{tool}.allow").write_text("root\n  claude-agent \n")
    r = deny(tmp_path, "claude-agent")
    assert r.returncode == 1
    assert f"{tool}.allow lists claude-agent" in r.stdout


def test_an_allow_file_without_the_user_is_fine(tmp_path):
    (tmp_path / "cron.allow").write_text("root\ncodex-other\n")
    r = deny(tmp_path, "codex")
    assert r.returncode == 0, r.stdout
    assert (tmp_path / "cron.deny").read_text() == "codex\n"


@pytest.mark.parametrize("user", ["root", "runner", "", "codex\nroot"])
def test_deny_refuses_an_unknown_user(tmp_path, user):
    r = deny(tmp_path, user)
    assert r.returncode == 1 and "user must be codex or claude-agent" in r.stdout
    assert list(tmp_path.iterdir()) == []


def test_create_mode_denies_right_after_the_user_exists():
    first = composite_runs(CREATE)[0]
    assert first.index('sudo usermod -a -G runner "$AGENT_USER"') < first.index('sudo bash "$DENY_SCRIPT" "$AGENT_USER"')
    assert "        DENY_SCRIPT: ${{ github.action_path }}/deny-schedulers.sh\n" in CREATE.read_text()
    assert os.access(DENY, os.X_OK)
    # The path smoke runs the same block with the step's env.
    smoke = (ROOT / "tests" / "codex_path_smoke.sh").read_text()
    assert 'DENY_SCRIPT="$root/.github/actions/create-codex-user/deny-schedulers.sh"' in smoke
