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
  `create-codex-user` step and before the codex-action step; between
  provisioning and codex-action the `Reset codex home` step (the same
  composite, `mode: reset-home`) kills every codex process and re-creates
  the codex home, so nothing a hostile build backend left behind reaches
  codex-action's runner-side reads of that home (review round 1); the
  runner writes nothing into the workspace after the codex user exists
  (the prompt file lives in RUNNER_TEMP, the exclude lines are appended
  before the user is created); the caller's own recipe (`provision`, or
  the earlier `codex_provision` when `provision` is empty) reaches the
  provisioning step as the composite's `recipe`, and neither input reaches
  the Claude job yet (design/executed-paths-residual.md, plan step 1).
- The `provision-fallback` composite runs its recipe under `sudo -u <user>
  -H` from a copy in `$RUNNER_TEMP` when `user` is set, directly as the
  runner otherwise; a caller recipe replaces the default install after the
  uv bootstrap; the recipe appends to `GITHUB_PATH` only when that file is
  there (the runner case). `create-codex-user`'s `codex-home.sh` re-creates
  the home over whatever is there, and its reset mode refuses to proceed
  while codex processes survive the kill loop.
"""

import os
import re
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
    # `provision` wins; the earlier `codex_provision` is the fallback when it
    # is empty (design/executed-paths-residual.md → Provisioning).
    assert "          recipe: ${{ inputs.provision || inputs.codex_provision }}\n" in provision
    # Gate: a Python project (the generic recipe) OR a caller recipe under
    # either name — a Node repository's recipe must run without a
    # pyproject.toml (review round 2); the reviewer keeps its `ok` clause.
    cond = next(l for l in provision.splitlines() if l.startswith("        if: "))
    recipe_set = "hashFiles('pyproject.toml') != '' || inputs.provision != '' || inputs.codex_provision != ''"
    if name == "claude-review.yml":
        assert cond == f"        if: needs.gate.outputs.ok == 'true' && ({recipe_set})"
    else:
        assert cond == f"        if: {recipe_set}"
    order = [s for s in steps(codex_job) if any(k in s for k in ("create-codex-user@main", "provision-fallback@main", CODEX_ACTION))]
    assert [("create-codex-user@main" in order[0], "provision-fallback@main" in order[1],
             "create-codex-user@main" in order[2], CODEX_ACTION in order[3])] == [(True, True, True, True)]
    assert "\n        id: codexuser\n" in order[0] and "\n        id: setup\n" in order[1]
    assert "\n        id: codexhome\n" in order[2] and "        with:\n          mode: reset-home\n" in order[2]
    assert "mode:" not in order[0]
    surface = step_with(codex_job, "\n        id: surface\n")
    assert "          CODEXHOME_OUTCOME: ${{ steps.codexhome.outcome }}\n" in surface
    assert '[ "${CODEXHOME_OUTCOME:-}" = "failure" ]' in surface
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
    compose = step_with(codex_job, 'for t in pytest ruff mypy pyright python3 node pnpm npm; do')
    assert "          BIN: ${{ steps.setup.outputs.bin }}\n" in compose
    assert "          SETUP_OUTCOME: ${{ steps.setup.outcome }}\n" in compose
    assert 'IFS=: read -r -a dirs <<<"$BIN"' in compose
    assert '[ -x "$d/$t" ] && { tools="$tools$t=$d/$t "; break; }' in compose
    assert "command -v" not in "\n".join(code_lines(compose))


@pytest.mark.parametrize("name", REUSABLE)
def test_the_runner_writes_nothing_into_the_workspace_after_the_codex_user_exists(name):
    """After `Create codex user` a process running as codex may have planted
    symlinks in the group-writable workspace, so every runner-side write
    between that step and the reclaim goes to RUNNER_TEMP: the prompt file,
    and the `.git/info/exclude` lines are appended before the user exists."""
    codex_job = jobs(workflow_text(name))[AGENT_JOBS[name][1]]
    all_steps = steps(codex_job)
    user_at = next(i for i, s in enumerate(all_steps) if "\n        id: codexuser\n" in s)
    codex_at = next(i for i, s in enumerate(all_steps) if CODEX_ACTION in s)
    for s in all_steps[user_at + 1: codex_at]:
        code = "\n".join(code_lines(s))
        assert ".codex-prompt.md" not in code and ">>.git/info/exclude" not in code, s[:60]
        # No redirection into a dot-relative path (the prompt file used to
        # be `>.codex-prompt.md`; the exclude append `>>.git/info/exclude`).
        assert not re.search(r'>>?\s*"?\.(git|codex|venv)', code), s[:60]
    assert "prompt-file: ${{ runner.temp }}/" in all_steps[codex_at]
    if name != "claude-review.yml":
        prep = step_with(codex_job, "\n        id: codexprep\n")
        assert ">>.git/info/exclude" in prep and ".codex-prompt.md" not in prep
        assert all_steps.index(prep) < user_at


@pytest.mark.parametrize("name", REUSABLE)
@pytest.mark.parametrize("input_name", ["provision", "codex_provision"])
def test_the_caller_recipe_input_is_declared_and_reaches_only_the_codex_job(name, input_name):
    text = workflow_text(name)
    decl = text[text.index(f"      {input_name}:\n"):]
    decl = decl[:re.search(r"\n      [a-z_]+:\n", decl).start()]
    assert "        required: false\n" in decl and "        type: string\n" in decl and decl.endswith('        default: ""')
    ref = re.compile(rf"\binputs\.{input_name}\b")
    for job, block in jobs(text).items():
        uses = [l.strip() for l in code_lines(block) if ref.search(l)]
        if job == AGENT_JOBS[name][1]:
            # The provisioning step's gate and its `recipe` input, nothing else.
            assert len(uses) == 2 and uses[1] == "recipe: ${{ inputs.provision || inputs.codex_provision }}", uses
            assert uses[0].startswith("if: ") and f"inputs.{input_name} != ''" in uses[0], uses
        else:
            # No other job: the Claude job keeps claude-setup until plan step 5.
            assert uses == [], job


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
    assert ("    value: ${{ github.workspace }}/.venv/bin:${{ github.workspace }}/node_modules/.bin"
            "${{ inputs.user != '' && format(':/home/{0}/.local/bin', inputs.user) || '' }}\n") in text
    assert "\n  recipe:\n" in text[text.index("\ninputs:\n"):text.index("\nruns:\n")]
    return lift_run(text, "    - shell: bash")


def write_exe(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def dispatch(tmp_path: Path, user: str, caller_recipe: str = ""):
    """Run the composite's step with a stub sudo and a stub recipe that
    records who ran it, from where and with which arguments."""
    bins = tmp_path / "bin"
    bins.mkdir()
    log = tmp_path / "log"
    # Records the full argv, then runs the command as the caller: `-u X -H
    # --` and the explicit `env -i VAR=…` prefix are swallowed so the
    # recipe stub keeps the test's LOG variable.
    write_exe(bins / "sudo", '#!/usr/bin/env bash\nprintf "sudo %s\\n" "$*" >>"$LOG"\n'
              'while [ "$#" -gt 0 ]; do case "$1" in -u) shift 2 ;; -H|--|env|-i|*=*) shift ;; *) break ;; esac; done\nexec "$@"\n')
    recipe = write_exe(tmp_path / "recipe.sh", '#!/usr/bin/env bash\nprintf "recipe %s %s%s\\n" "$0" "$(pwd)" "${1:+ arg=$1}" >>"$LOG"\n')
    temp = tmp_path / "runner-temp"
    temp.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    env = {"PATH": f"{bins}:{os.environ['PATH']}", "LOG": str(log), "AS_USER": user, "RECIPE": str(recipe),
           "RUNNER_TEMP": str(temp), "HOME": str(tmp_path), "CALLER_RECIPE": caller_recipe}
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
    assert log == f"sudo -u codex -H -- env -i HOME=/home/codex USER=codex LOGNAME=codex LANG=C.UTF-8 PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin bash {copy}\nrecipe {copy} {tmp_path / 'work'}\n"
    assert "provisioning as codex" in r.stdout


def test_composite_hands_the_caller_recipe_to_the_script_as_a_runner_temp_file(tmp_path):
    r, log, temp = dispatch(tmp_path, "codex", caller_recipe="uv venv --python 3.11\nuv sync --dev\n")
    assert r.returncode == 0, r.stderr
    caller = temp / "provision-recipe.sh"
    assert caller.read_text().rstrip("\n") == "uv venv --python 3.11\nuv sync --dev" and stat.S_IMODE(caller.stat().st_mode) == 0o644
    copy = temp / "provision-fallback.sh"
    assert log == f"sudo -u codex -H -- env -i HOME=/home/codex USER=codex LOGNAME=codex LANG=C.UTF-8 PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin bash {copy} {caller}\nrecipe {copy} {tmp_path / 'work'} arg={caller}\n"


def test_composite_refuses_a_caller_recipe_that_is_not_bash(tmp_path):
    r, log, temp = dispatch(tmp_path, "codex", caller_recipe="if [ x; then\n")
    assert r.returncode != 0 and log == ""


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


def recipe_run(tmp_path: Path, *, github_path: bool, pyproject: str, caller_recipe: str = None, check: bool = True):
    """Run provision.sh with the network and uv stubbed: the uv installer
    curl prints an empty script, `uv` records its arguments; `python3` is
    the interpreter running the tests (tomllib needs 3.11)."""
    bins = tmp_path / "bin"
    bins.mkdir(parents=True)
    log = tmp_path / "uv.log"
    write_exe(bins / "curl", "#!/usr/bin/env bash\nexit 0\n")
    write_exe(bins / "uv", '#!/usr/bin/env bash\nprintf "uv %s\\n" "$*" >>"$UV_LOG"\n')
    write_exe(bins / "python3", f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
    work = tmp_path / "work"
    (work / ".git" / "info").mkdir(parents=True)
    if pyproject is not None:
        (work / "pyproject.toml").write_text(pyproject)
    gh_path = tmp_path / "github_path"
    env = {"PATH": f"{bins}:{os.environ['PATH']}", "UV_LOG": str(log), "HOME": str(tmp_path)}
    if github_path:
        gh_path.write_text("")
        env["GITHUB_PATH"] = str(gh_path)
    args = []
    if caller_recipe is not None:
        (tmp_path / "caller.sh").write_text(caller_recipe)
        args = [str(tmp_path / "caller.sh")]
    r = sh("bash", str(COMPOSITE / "provision.sh"), *args, cwd=work, check=False, env=env)
    if check:
        assert r.returncode == 0, r.stderr
        return log.read_text(), gh_path, work
    return r, log.read_text() if log.exists() else "", work


def test_recipe_runs_the_caller_script_instead_of_the_default_install(tmp_path):
    log, gh_path, work = recipe_run(tmp_path, github_path=False, pyproject='[project]\nname = "x"\n',
                                    caller_recipe='uv venv --python 3.11\nuv sync --dev\necho "cwd=$(pwd) home=$HOME" >>"$UV_LOG"\n')
    # uv is bootstrapped and on PATH first; the caller's commands replace
    # the venv + dev-install; the exclude lines are written either way.
    assert log == f"uv venv --python 3.11\nuv sync --dev\ncwd={work} home={tmp_path}\n"
    assert (work / ".git" / "info" / "exclude").read_text() == ".venv/\n*.egg-info/\n"
    assert not gh_path.exists()


def test_recipe_installs_the_checkout_and_appends_path_only_as_the_runner(tmp_path):
    log, gh_path, work = recipe_run(tmp_path, github_path=True, pyproject='[project]\nname = "x"\n')
    assert log == "uv venv\nuv pip install -e .[dev]\n"
    assert gh_path.read_text() == f"{tmp_path}/.local/bin\n{work}/.venv/bin\n"
    assert (work / ".git" / "info" / "exclude").read_text() == ".venv/\n*.egg-info/\n"


def test_recipe_runs_a_caller_script_without_a_pyproject(tmp_path):
    # A Node repository: no pyproject.toml, the caller's recipe does the
    # provisioning (review round 2 — the workflows gate the step on either).
    log, _, work = recipe_run(tmp_path, github_path=False, pyproject=None,
                              caller_recipe='echo "pnpm install --frozen-lockfile" >>"$UV_LOG"\n')
    assert log == "pnpm install --frozen-lockfile\n"
    assert not (work / "pyproject.toml").exists()


def test_recipe_fails_fast_like_a_bash_step(tmp_path):
    # An intermediate failure fails the recipe (review round 2: a plain
    # `bash "$recipe"` returned 0 through `false` then `printf`).
    r, log, _ = recipe_run(tmp_path / "a", github_path=False, pyproject=None, check=False,
                           caller_recipe='false\necho REACHED_AFTER_FAILURE >>"$UV_LOG"\n')
    assert r.returncode != 0 and "REACHED" not in log
    # A failed pipeline followed by a successful command fails too (pipefail).
    r, log, _ = recipe_run(tmp_path / "b", github_path=False, pyproject=None, check=False,
                           caller_recipe='false | cat\necho REACHED_AFTER_PIPE >>"$UV_LOG"\n')
    assert r.returncode != 0 and "REACHED" not in log
    # A caller's explicit handling of an expected failure is preserved.
    log, _, _ = recipe_run(tmp_path / "c", github_path=False, pyproject=None,
                           caller_recipe='false || echo "handled" >>"$UV_LOG"\necho done >>"$UV_LOG"\n')
    assert log == "handled\ndone\n"


def test_recipe_skips_the_path_append_under_sudo_and_adds_the_dev_group(tmp_path):
    log, gh_path, _ = recipe_run(tmp_path, github_path=False,
                                 pyproject='[project]\nname = "x"\n[dependency-groups]\ndev = ["pytest"]\n')
    assert log == "uv venv\nuv pip install -e .[dev] --group dev\n"
    assert not gh_path.exists()


def workflow_text(name: str) -> str:
    return (WORKFLOWS / name).read_text()


# --- create-codex-user: the home recipe and the reset-home boundary ---------

CODEX_USER = ROOT / ".github" / "actions" / "create-codex-user"


def fake_sudo(bins: Path, *, pkill_rc: str = "1") -> Path:
    """A `sudo` that runs the command as the caller, records `pkill`
    invocations and answers them with PKILL_RC (1 = nothing to kill), and
    a `pkill` that does nothing else."""
    write_exe(bins / "sudo", '#!/usr/bin/env bash\n'
              'while [ "$#" -gt 0 ]; do case "$1" in -u) shift 2 ;; -H|--) shift ;; *) break ;; esac; done\n'
              'if [ "$1" = pkill ]; then printf "pkill %s\\n" "$*" >>"$LOG"; exit "$PKILL_RC"; fi\n'
              'exec "$@"\n')
    # `install -d -o codex -g codex -m MODE dir` without the codex account:
    # the ownership flags are dropped, the directory and mode are real.
    write_exe(bins / "install", '#!/usr/bin/env bash\n'
              'mode=755; while [ "$#" -gt 1 ]; do case "$1" in -d) shift ;; -o|-g) shift 2 ;; -m) mode=$2; shift 2 ;; *) break ;; esac; done\n'
              'mkdir -p "$1" && chmod "$mode" "$1"\n')
    return bins


def home_env(tmp_path: Path, **extra):
    bins = tmp_path / "bin"
    bins.mkdir(exist_ok=True)
    fake_sudo(bins)
    return {"PATH": f"{bins}:{os.environ['PATH']}", "LOG": str(tmp_path / "log"), "PKILL_RC": "1",
            "GITHUB_RUN_ID": "4242", "HOME_ROOT": str(tmp_path), **extra}


def run_home_script(tmp_path: Path, env: dict):
    """codex-home.sh addresses /home/<user>; the test rewrites that prefix to
    a scratch root through a copy of the script (the logic is the point, not
    the literal path)."""
    script = (CODEX_USER / "codex-home.sh").read_text().replace('home="/home/$user"', 'home="$HOME_ROOT/home/$user"')
    (tmp_path / "home" / "codex").mkdir(parents=True, exist_ok=True)
    return sh("bash", "-c", script + "\n", "codex-home", "codex", check=False, env=env)


def test_codex_home_script_replaces_a_planted_symlink_with_the_profile(tmp_path):
    codex_home = tmp_path / "home" / "codex" / ".codex"
    codex_home.mkdir(parents=True)
    marker = tmp_path / "environ"
    marker.write_text("SECRET=1\n")
    (codex_home / "config.toml").symlink_to(marker)          # the B1 primitive
    (codex_home / "planted").write_text("x")
    r = run_home_script(tmp_path, home_env(tmp_path))
    assert r.returncode == 0, r.stderr
    cfg = codex_home / "config.toml"
    assert cfg.is_file() and not cfg.is_symlink()
    assert cfg.read_text().splitlines()[1:] == ['[permissions.workspace_net]', 'extends = ":workspace"',
                                                '[permissions.workspace_net.workspace_roots]', '"." = true',
                                                '[permissions.workspace_net.network]', 'enabled = true']
    assert not (codex_home / "planted").exists()
    assert (codex_home / "4242.json").exists() and stat.S_IMODE((codex_home / "4242.json").stat().st_mode) == 0o666
    assert marker.read_text() == "SECRET=1\n"                 # rm -rf removed the link, not its target


def reset_step() -> str:
    text = (CODEX_USER / "action.yml").read_text()
    return lift_run(text, "      if: inputs.mode == 'reset-home'")


def test_reset_mode_kills_codex_processes_then_recreates_the_home(tmp_path):
    # SYSTEM_PATH: the step pins PATH to it before its first command; the
    # test points it at the stubs (and the real bash/sleep).
    env = home_env(tmp_path, HOME_SCRIPT=str(tmp_path / "home.sh"), SYSTEM_PATH=f"{tmp_path / 'bin'}:/usr/bin:/bin")
    (tmp_path / "home.sh").write_text('#!/usr/bin/env bash\nprintf "home %s\\n" "$1" >>"$LOG"\n')
    bins = tmp_path / "bin"
    write_exe(bins / "id", "#!/usr/bin/env bash\nexit 0\n")
    r = sh("bash", "-eo", "pipefail", "-c", reset_step(), check=False, env=env)
    assert r.returncode == 0, r.stderr + r.stdout
    assert (tmp_path / "log").read_text() == "pkill pkill -KILL -u codex\nhome codex\n"


def test_reset_mode_refuses_while_codex_processes_survive(tmp_path):
    env = home_env(tmp_path, HOME_SCRIPT=str(tmp_path / "home.sh"), PKILL_RC="0",   # always "killed something"
                   SYSTEM_PATH=f"{tmp_path / 'bin'}:/usr/bin:/bin")
    (tmp_path / "home.sh").write_text('#!/usr/bin/env bash\nprintf "home %s\\n" "$1" >>"$LOG"\n')
    write_exe(tmp_path / "bin" / "id", "#!/usr/bin/env bash\nexit 0\n")
    r = sh("bash", "-eo", "pipefail", "-c", reset_step(), check=False, env=env)
    assert r.returncode != 0
    log = (tmp_path / "log").read_text().splitlines()
    assert log.count("pkill pkill -KILL -u codex") == 10 and "home codex" not in log
    assert "still running as codex after repeated kills" in r.stdout + r.stderr


def test_create_mode_runs_the_same_home_script_last():
    text = (CODEX_USER / "action.yml").read_text()
    # The create mode's last step (the grant, after the PATH check) ends
    # with the shared home script.
    runs = text[text.index("\nruns:\n"):]
    grant_at = runs.index("# 2, continued: the workspace grant.")
    create = runs[grant_at:runs.index("\n    - ", grant_at)]
    create = "\n".join(l for l in create.splitlines() if not l.startswith("    #"))  # the next step's comment
    assert create.rstrip().endswith('bash "$HOME_SCRIPT" codex')
    assert "config.toml" not in create and "permissions.workspace_net" not in create
    assert "\n  mode:\n" in text and "    default: create\n" in text
