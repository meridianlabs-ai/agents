"""Agent jobs get read-only Actions cache access (Claude Security 4629157;
design/agent-cache-scope.md).

Every reusable agent workflow declares top-level `cache-mode: read`, which
GitHub enforces on the token of each of its jobs: an agent job can restore
the caller's caches but never save one, in any scope, on any trigger,
whatever the caller's claude-setup nests and whatever the agent runs. These
are structural checks on the workflow text (PyYAML is not a test
dependency), one per rule the design relies on:

- each of the four agent workflows has exactly one column-0
  `cache-mode: read`, before `jobs:`, and no job in them sets its own
  `cache-mode` (a job-level key would override the workflow's);
- no `cache-mode` in `.github/workflows/` or `examples/` is anything but
  `read` or `none`, at any indentation, except the canary's positive
  control, listed by file and job;
- every calling job in the example stubs and this repo's own stubs caps its
  call at `cache-mode: read`, so a future write-capable declaration here
  would fail a copied caller's validation instead of being granted;
- the canary's called workflow declares the same line as the agent
  workflows, and its calling job, like a caller stub's, sets none.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
EXAMPLES = ROOT / "examples"

AGENT_WORKFLOWS = ["claude.yml", "claude-auto.yml", "claude-auto-review.yml", "claude-review.yml"]
STUBS = [
    EXAMPLES / "claude-stub.yml",
    EXAMPLES / "claude-auto-stub.yml",
    EXAMPLES / "claude-review-stub.yml",
    WORKFLOWS / "claude-stub.yml",
    WORKFLOWS / "claude-auto-stub.yml",
    WORKFLOWS / "claude-review-stub.yml",
]
CANARY = "cache-mode-canary.yml"
CANARY_REUSABLE = "cache-mode-canary-reusable.yml"

# (file, job) → the one write-capable mode allowed: the canary's positive
# control, which must be able to save for the canary to see a save.
WRITE_EXCEPTIONS = {(CANARY, "control"): "write"}

CACHE_MODE = re.compile(r"^(?P<indent>\s*)cache-mode\s*:\s*(?P<value>.*?)\s*(?:#.*)?$")
JOB = re.compile(r"^  (?P<job>[A-Za-z_][A-Za-z0-9_-]*):\s*(?:#.*)?$")


def cache_mode_lines(path):
    """Every `cache-mode:` key in the file: (line number, indent, value, job).

    `job` is the job whose block the line sits in (None above `jobs:`).
    Comment lines are skipped; a key inside a block scalar would not be one,
    and no workflow here carries the text in one.
    """
    found, job, in_jobs = [], None, False
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if line.startswith("jobs:"):
            in_jobs = True
            continue
        if in_jobs and (m := JOB.match(line)):
            job = m["job"]
            continue
        if m := CACHE_MODE.match(line):
            value = m["value"].strip("'\"")
            found.append((n, len(m["indent"]), value, job))
    return found


def top_level_keys(path):
    return [(n, line.split(":", 1)[0]) for n, line in enumerate(path.read_text().splitlines(), 1)
            if re.match(r"^[A-Za-z][A-Za-z0-9_-]*:", line)]


def calling_jobs(path):
    """Jobs of a stub that call an agent workflow: {job: body lines}."""
    jobs, job, in_jobs = {}, None, False
    for line in path.read_text().splitlines():
        if line.startswith("jobs:"):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if m := JOB.match(line):
            job = m["job"]
            jobs[job] = []
        elif job:
            jobs[job].append(line)
    return {j: body for j, body in jobs.items()
            if any(re.match(r"^    uses: meridianlabs-ai/agents/\.github/workflows/", l) for l in body)}


@pytest.mark.parametrize("name", AGENT_WORKFLOWS + [CANARY_REUSABLE])
def test_workflow_declares_read_before_jobs(name):
    path = WORKFLOWS / name
    lines = cache_mode_lines(path)
    top = [(n, value) for n, indent, value, _ in lines if indent == 0]
    assert len(top) == 1 and top[0][1] == "read", (
        f"{name}: want exactly one column-0 `cache-mode: read`, got {top}")
    jobs_line = next(n for n, key in top_level_keys(path) if key == "jobs")
    assert top[0][0] < jobs_line, f"{name}: `cache-mode` must precede `jobs:` (line {jobs_line})"


@pytest.mark.parametrize("name", AGENT_WORKFLOWS + [CANARY_REUSABLE])
def test_no_job_overrides_the_workflow_key(name):
    overrides = [(n, job, value) for n, indent, value, job in cache_mode_lines(WORKFLOWS / name) if indent]
    assert not overrides, f"{name}: a job-level `cache-mode` overrides the workflow's: {overrides}"


def test_no_write_capable_cache_mode_anywhere():
    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml")) + sorted(EXAMPLES.glob("*.yml"))
    seen_exceptions, bad = set(), []
    for path in files:
        for n, _, value, job in cache_mode_lines(path):
            key = (path.name, job)
            if path.parent == WORKFLOWS and WRITE_EXCEPTIONS.get(key) == value:
                seen_exceptions.add(key)
                continue
            if value not in ("read", "none"):
                bad.append(f"{path.relative_to(ROOT)}:{n} (job {job}): cache-mode {value!r}")
    assert not bad, "write-capable or non-literal cache-mode:\n" + "\n".join(bad)
    # The exception list names real lines; a stale entry would hide nothing.
    assert seen_exceptions == set(WRITE_EXCEPTIONS)


@pytest.mark.parametrize("path", STUBS, ids=lambda p: str(p.relative_to(ROOT)))
def test_stub_calling_jobs_cap_at_read(path):
    jobs = calling_jobs(path)
    assert jobs, f"{path.name}: no job calls an agent workflow"
    for job, body in jobs.items():
        modes = [l for l in body if re.match(r"^    cache-mode\s*:", l)]
        assert modes == ["    cache-mode: read"], f"{path.name} job {job}: want `cache-mode: read`, got {modes}"


def test_canary_calls_like_a_stub():
    """The probe's calling job sets no mode, so only the called workflow's key
    limits it — the shape of every caller stub's call."""
    lines = cache_mode_lines(WORKFLOWS / CANARY)
    assert not [l for l in lines if l[3] == "probe"]
    assert not [l for l in lines if l[1] == 0], "the canary's caller must not set a workflow-level mode"
    text = (WORKFLOWS / CANARY).read_text()
    assert f"    uses: ./.github/workflows/{CANARY_REUSABLE}\n" in text
    assert "\non:\n  workflow_dispatch:\n\n" in text, "the canary is dispatch-only"
