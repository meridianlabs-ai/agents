"""One untrusted job per engine (Claude Security findings 4628446 and
4629153, 2026-09-22): structural checks on the four reusable workflows and
tests of the `provision-fallback` composite's `user` dispatch.

The platform facts these rest on, verified against actions/runner (2026-09-22):
the runner builds its `secrets` expression context from the job message
before any step runs (`ExecutionContext.InitializeJob`:
`ExpressionValues["secrets"] = Global.Variables.ToSecretsContext()`), and a
step's `if:` is evaluated by the runner when the step is reached
(`StepsRunner`, `EvaluateStepIf`). So a secret any step of a job references
is delivered to that job's runner whether or not the step runs, and the only
way to keep `OPENAI_API_KEY` out of a Claude-engine run is a job that does
not reference it: a job-level `if:` is decided by the service before the
job is dispatched, and a skipped job gets no job message at all.

- 4629153: the job that runs the Claude agent names no `OPENAI_API_KEY`;
  the codex job names it at its codex-action step and nowhere else; the
  two are selected by the gate's `engine` output at the job level and
  never both run; the land job waits for both.
- 4628446: the codex job executes nothing from the checked-out tree as the
  runner — no `./.github/actions/claude-setup`, and provisioning runs the
  shared fallback recipe as the `codex` user (`user: codex`), after the
  `create-codex-user` step and before the codex-action step.
- The `provision-fallback` composite runs its recipe under `sudo -u <user>
  -H` from a copy in `$RUNNER_TEMP` when `user` is set, directly as the
  runner otherwise; the recipe appends to `GITHUB_PATH` only when that file
  is there (the runner case).
"""

import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
COMPOSITE = ROOT / ".github" / "actions" / "provision-fallback"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_app_token_minting import AGENT_JOBS, REUSABLE, code_lines, jobs, steps  # noqa: E402
from test_land_helpers import lift_run, sh, step_block  # noqa: E402

CODEX_ACTION = "openai/codex-action@v1"
CLAUDE_ACTION = "anthropics/claude-code-action@v1"
KEY = "OPENAI_API_KEY"
LAND_NEEDS = {"claude.yml": "needs: [gate, agent, agent-codex]", "claude-review.yml": "needs: [gate, review, review-codex]",
              "claude-auto.yml": "needs: [gate, fix, fix-codex]", "claude-auto-review.yml": "needs: [gate, fix, fix-codex]"}


def job_if(job: str) -> str:
    return next(line for line in job.splitlines() if line.startswith("    if: "))


def step_with(job: str, needle: str) -> str:
    found = [s for s in steps(job) if needle in s]
    assert len(found) == 1, (needle, len(found))
    return found[0]


# --- 4629153: the key lives in the codex job alone ---------------------------


@pytest.mark.parametrize("name", REUSABLE)
def test_the_claude_job_names_no_openai_key_and_runs_no_codex(name):
    claude_job = jobs(workflow_text(name))[AGENT_JOBS[name][0]]
    assert not [line for line in code_lines(claude_job) if KEY in line]
    assert CODEX_ACTION not in claude_job and "create-codex-user" not in claude_job
    assert CLAUDE_ACTION in claude_job


@pytest.mark.parametrize("name", REUSABLE)
def test_the_codex_job_names_the_key_only_at_the_codex_action_step(name):
    text = workflow_text(name)
    codex_job = jobs(text)[AGENT_JOBS[name][1]]
    named = [line for line in code_lines(codex_job) if f"secrets.{KEY}" in line]
    assert named == ["          openai-api-key: ${{ secrets.OPENAI_API_KEY }}"]
    assert named[0] in step_with(codex_job, CODEX_ACTION)
    # The only other mention is the Surface step's error text naming a
    # missing key as a cause — prose in a string, no reference.
    assert all('err="' in line for line in code_lines(codex_job) if KEY in line and line not in named)
    assert CLAUDE_ACTION not in codex_job
    # And no other job of the workflow references it at all: the declaration
    # under workflow_call stays (callers pass the secret; a stub naming an
    # undeclared secret fails to load), the gate and land jobs never did.
    for job, block in jobs(text).items():
        if job != AGENT_JOBS[name][1]:
            assert not [line for line in code_lines(block) if f"secrets.{KEY}" in line], job
    assert f"      {KEY}:\n" in text[: text.index("\njobs:\n")]


@pytest.mark.parametrize("name", REUSABLE)
def test_the_two_agent_jobs_are_selected_by_the_gate_engine_and_never_both(name):
    text = workflow_text(name)
    claude_job, codex_job = (jobs(text)[j] for j in AGENT_JOBS[name])
    assert "needs.gate.outputs.engine != 'codex'" in job_if(claude_job)
    assert "needs.gate.outputs.engine == 'codex'" in job_if(codex_job)
    assert "    needs: gate\n" in claude_job and "    needs: gate\n" in codex_job
    # Same untrusted permissions block on both.
    perms = lambda job: job[job.index("    permissions:\n"):job.index("    steps:\n")]  # noqa: E731
    assert [l.split("#")[0].strip() for l in perms(claude_job).splitlines() if l.strip().startswith(("contents", "pull-requests", "issues", "id-token", "actions"))] == \
           [l.split("#")[0].strip() for l in perms(codex_job).splitlines() if l.strip().startswith(("contents", "pull-requests", "issues", "id-token", "actions"))]
    for want in ("contents: read", "pull-requests: read", "issues: read", "id-token: write", "actions: read"):
        assert want in perms(codex_job)
    # No step of either job gates on the engine any more: the job does.
    for job in (claude_job, codex_job):
        assert "needs.gate.outputs.engine" not in "\n".join(l for l in code_lines(job) if l.startswith("        if:") or "if: >-" in l or l.startswith("          needs.gate.outputs.engine")), name


@pytest.mark.parametrize("name", REUSABLE)
def test_the_land_job_waits_for_both_agent_jobs(name):
    land = jobs(workflow_text(name))["land"]
    assert f"    {LAND_NEEDS[name]}\n" in land
    codex_job = AGENT_JOBS[name][1]
    if name == "claude-review.yml":
        # The reviewer's land job skips only on a cancelled review, whichever
        # engine's job ran; the landed-review check reads the Claude job's
        # outputs, empty when the codex job ran.
        assert f"needs.{codex_job}.result != 'cancelled'" in job_if(land)
    else:
        claude_job = AGENT_JOBS[name][0]
        assert (f"      AGENT_RESULT: ${{{{ needs.gate.outputs.engine == 'codex' && needs.{codex_job}.result "
                f"|| needs.{claude_job}.result }}}}\n") in land
        assert "needs.fix.result" not in land.replace("needs.fix.result }}", "") if claude_job == "fix" else True
        for line in code_lines(land):
            if line.startswith("        if:") or line.startswith("          needs."):
                assert f"needs.{claude_job}." not in line and f"needs.{codex_job}." not in line, line


# --- 4628446: nothing from the tree runs as the runner in the codex job -------


@pytest.mark.parametrize("name", REUSABLE)
def test_the_codex_job_provisions_as_the_codex_user_after_the_boundary(name):
    codex_job = jobs(workflow_text(name))[AGENT_JOBS[name][1]]
    assert "uses: ./" not in codex_job, "a local action runs the checkout's code as the runner"
    assert "claude-setup" not in "\n".join(code_lines(codex_job))
    provision = step_with(codex_job, "provision-fallback@main")
    assert "        with:\n          user: codex\n" in provision
    assert "        if: " in provision and "hashFiles('pyproject.toml') != ''" in provision
    order = [s for s in steps(codex_job) if any(k in s for k in ("create-codex-user@main", "provision-fallback@main", CODEX_ACTION))]
    assert ["create-codex-user@main" in order[0], "provision-fallback@main" in order[1], CODEX_ACTION in order[2]] == [True, True, True]
    assert "\n        id: codexuser\n" in order[0] and "\n        id: setup\n" in order[1]
    # Every step between the user boundary and codex is the provisioning
    # itself or a runner-side composition step (`run:` blocks and shared
    # composites from this repository): none of them `uses:` an action from
    # the checkout.
    all_steps = steps(codex_job)
    between = all_steps[all_steps.index(order[0]) + 1: all_steps.index(order[2])]
    for s in between:
        uses = [l.strip() for l in code_lines(s) if l.strip().startswith("uses: ")]
        assert all(u.startswith("uses: meridianlabs-ai/agents/.github/actions/") for u in uses), (s[:80], uses)


@pytest.mark.parametrize("name", REUSABLE)
def test_the_codex_prompts_take_tool_paths_from_the_provisioned_venv(name):
    codex_job = jobs(workflow_text(name))[AGENT_JOBS[name][1]]
    compose = step_with(codex_job, 'for t in pytest ruff mypy pyright python3; do')
    assert "          BIN: ${{ steps.setup.outputs.bin }}\n" in compose
    assert "          SETUP_OUTCOME: ${{ steps.setup.outcome }}\n" in compose
    assert '[ -x "$BIN/$t" ] && tools="$tools$t=$BIN/$t "' in compose
    assert "command -v" not in "\n".join(code_lines(compose))


@pytest.mark.parametrize("name", REUSABLE)
def test_the_claude_job_keeps_the_runner_side_provisioning(name):
    claude_job = jobs(workflow_text(name))[AGENT_JOBS[name][0]]
    shim = step_with(claude_job, "uses: ./.github/actions/claude-setup")
    assert "hashFiles('.github/actions/claude-setup/action.yml', '.github/actions/claude-setup/action.yaml') != ''" in shim
    fallback = step_with(claude_job, "provision-fallback@main")
    assert "user:" not in fallback and "hashFiles('pyproject.toml') != ''" in fallback


# --- the composite -------------------------------------------------------------


def composite_step() -> str:
    text = (COMPOSITE / "action.yml").read_text()
    assert "\n  user:\n" in text[text.index("\ninputs:\n"):text.index("\nruns:\n")]
    assert "    value: ${{ github.workspace }}/.venv/bin\n" in text
    return lift_run(text, "    - shell: bash")


def write_exe(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def dispatch(tmp_path: Path, user: str):
    """Run the composite's step with a stub sudo and a stub recipe that
    records who ran it and from where."""
    bins = tmp_path / "bin"
    bins.mkdir()
    log = tmp_path / "log"
    write_exe(bins / "sudo", '#!/usr/bin/env bash\nprintf "sudo %s\\n" "$*" >>"$LOG"\n'
              'while [ "$#" -gt 0 ]; do case "$1" in -u) shift 2 ;; -H) shift ;; *) break ;; esac; done\nexec "$@"\n')
    recipe = write_exe(tmp_path / "recipe.sh", '#!/usr/bin/env bash\nprintf "recipe %s %s\\n" "$0" "$(pwd)" >>"$LOG"\n')
    temp = tmp_path / "runner-temp"
    temp.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    env = {"PATH": f"{bins}:{os.environ['PATH']}", "LOG": str(log), "AS_USER": user, "RECIPE": str(recipe),
           "RUNNER_TEMP": str(temp), "HOME": str(tmp_path)}
    r = sh("bash", "-eo", "pipefail", "-c", composite_step(), cwd=work, check=False, env=env)
    return r, log.read_text() if log.exists() else "", temp


def test_composite_runs_the_recipe_as_the_runner_without_a_user(tmp_path):
    r, log, temp = dispatch(tmp_path, "")
    assert r.returncode == 0, r.stderr
    assert log == f"recipe {tmp_path / 'recipe.sh'} {tmp_path / 'work'}\n"
    assert not (temp / "provision-fallback.sh").exists()


def test_composite_runs_a_runner_temp_copy_under_sudo_for_the_user(tmp_path):
    r, log, temp = dispatch(tmp_path, "codex")
    assert r.returncode == 0, r.stderr
    copy = temp / "provision-fallback.sh"
    assert copy.exists() and stat.S_IMODE(copy.stat().st_mode) == 0o644
    assert copy.read_text() == (tmp_path / "recipe.sh").read_text()
    assert log == f"sudo -u codex -H bash {copy}\nrecipe {copy} {tmp_path / 'work'}\n"
    assert "provisioning as codex" in r.stdout


def test_composite_fails_when_sudo_fails(tmp_path):
    bins = tmp_path / "bin"
    bins.mkdir()
    write_exe(bins / "sudo", "#!/usr/bin/env bash\nexit 7\n")
    recipe = write_exe(tmp_path / "recipe.sh", "#!/usr/bin/env bash\n")
    temp = tmp_path / "runner-temp"
    temp.mkdir()
    env = {"PATH": f"{bins}:{os.environ['PATH']}", "AS_USER": "codex", "RECIPE": str(recipe), "RUNNER_TEMP": str(temp)}
    r = sh("bash", "-eo", "pipefail", "-c", composite_step(), cwd=tmp_path, check=False, env=env)
    assert r.returncode == 7


def recipe_run(tmp_path: Path, *, github_path: bool, pyproject: str):
    """Run provision.sh with the network and uv stubbed: the uv installer
    curl prints an empty script, `uv` records its arguments; `python3` is
    the interpreter running the tests (tomllib needs 3.11)."""
    bins = tmp_path / "bin"
    bins.mkdir()
    log = tmp_path / "uv.log"
    write_exe(bins / "curl", "#!/usr/bin/env bash\nexit 0\n")
    write_exe(bins / "uv", '#!/usr/bin/env bash\nprintf "uv %s\\n" "$*" >>"$UV_LOG"\n')
    write_exe(bins / "python3", f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
    work = tmp_path / "work"
    (work / ".git" / "info").mkdir(parents=True)
    (work / "pyproject.toml").write_text(pyproject)
    gh_path = tmp_path / "github_path"
    env = {"PATH": f"{bins}:{os.environ['PATH']}", "UV_LOG": str(log), "HOME": str(tmp_path)}
    if github_path:
        gh_path.write_text("")
        env["GITHUB_PATH"] = str(gh_path)
    r = sh("bash", str(COMPOSITE / "provision.sh"), cwd=work, check=False, env=env)
    assert r.returncode == 0, r.stderr
    return log.read_text(), gh_path, work


def test_recipe_installs_the_checkout_and_appends_path_only_as_the_runner(tmp_path):
    log, gh_path, work = recipe_run(tmp_path, github_path=True, pyproject='[project]\nname = "x"\n')
    assert log == "uv venv\nuv pip install -e .[dev]\n"
    assert gh_path.read_text() == f"{tmp_path}/.local/bin\n{work}/.venv/bin\n"
    assert (work / ".git" / "info" / "exclude").read_text() == ".venv/\n*.egg-info/\n"


def test_recipe_skips_the_path_append_under_sudo_and_adds_the_dev_group(tmp_path):
    log, gh_path, _ = recipe_run(tmp_path, github_path=False,
                                 pyproject='[project]\nname = "x"\n[dependency-groups]\ndev = ["pytest"]\n')
    assert log == "uv venv\nuv pip install -e .[dev] --group dev\n"
    assert not gh_path.exists()


def workflow_text(name: str) -> str:
    return (WORKFLOWS / name).read_text()
