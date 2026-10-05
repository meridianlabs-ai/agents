"""Every claude-code-action step runs Claude Code in `default` permission mode
(issue #200).

With no `--permission-mode`, Claude Code 2.1.287 starts a headless run in auto
mode: Write and redirects into the working directory run, and commands no rule
matches go to a model classifier instead of being refused. `default` keeps the
`settings` allow and deny lists as the boundary. The flag comes first in
`claude_args`, so a caller's own `claude_args` (spliced later; the action keeps
the last value of a flag) may override it, and no stub or example does.
Text-based like the other structural checks: PyYAML is not a test dependency.
Run with `python3 -m pytest` from the repo root.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_app_token_minting import AGENT_JOBS, REUSABLE, code_lines, jobs, steps  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CLAUDE_ACTION = "anthropics/claude-code-action@v1"
FLAG = "--permission-mode default"


def claude_args(step: str) -> list:
    """The `claude_args: >-` lines of a step, stripped, comments left out."""
    lines = code_lines(step)
    start = lines.index("          claude_args: >-") + 1
    args = []
    for line in lines[start:]:
        if not line.startswith("            "):
            break
        args.append(line.strip())
    return [a for a in args if a]


def claude_steps(job: str) -> list:
    """The job's steps that run claude-code-action (the canary's `if: false`
    download-only step does not)."""
    return [s for s in steps(job) if f"uses: {CLAUDE_ACTION}" in s and "        if: false\n" not in s]


@pytest.mark.parametrize("name", REUSABLE)
def test_the_claude_job_passes_default_permission_mode_first(name):
    found = claude_steps(jobs((WORKFLOWS / name).read_text())[AGENT_JOBS[name][0]])
    assert len(found) == 1
    args = claude_args(found[0])
    assert args[0] == FLAG
    assert sum("--permission-mode" in a for a in args) == 1
    if "${{ inputs.claude_args }}" in args:
        assert args.index("${{ inputs.claude_args }}") > args.index(FLAG)


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_every_claude_code_action_step_passes_default_permission_mode(path):
    for job in jobs(path.read_text()).values():
        if "    steps:\n" in job:
            for step in claude_steps(job):
                assert claude_args(step)[0] == FLAG, step[:120]


@pytest.mark.parametrize("path", sorted([*WORKFLOWS.glob("*-stub.yml"), *(ROOT / "examples").glob("*.yml")]),
                         ids=lambda p: p.name)
def test_no_stub_or_example_overrides_the_permission_mode(path):
    assert not [line for line in code_lines(path.read_text()) if "--permission-mode" in line]
