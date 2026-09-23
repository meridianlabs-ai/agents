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
  workflows, and its calling job, like a caller stub's, sets none;
- the canary's `Check` step, lifted and run against a stub `gh` and `curl`,
  is green only when exactly the control entry exists and restores, each
  job's "Set up job" line shows its mode, and the probe's log shows the
  mode-aware client's skip and the service's refusal.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_review_fix_gate import STEP_BASH, lift_step  # noqa: E402

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


# The canary's `verify` job decides green or red in its `Check` step, and the
# canary can only be dispatched from the default branch, so the step is lifted
# and run here against a stub `gh` (the cache listing, the attempt's jobs) and
# a stub `curl` (each job's log), the way the other workflow tests lift theirs.

PREFIX = "canary-7-1-"
PROBE_LOG = (
    "\ufeff2026-09-23T10:00:00Z ##[group]Runner Image\n"
    "2026-09-23T10:00:00Z Cache mode: read\n"
    "2026-09-23T10:00:05Z Cache save skipped: the effective cache-mode 'read' does not permit writes.\n"
    "2026-09-23T10:00:06Z ##[warning]Failed to save: Unable to reserve cache with key canary-7-1-old."
    " More details: cache write denied: token is read-only\n"
)
CONTROL_LOG = "2026-09-23T10:00:00Z Cache mode: write\n2026-09-23T10:00:05Z Cache saved with key: canary-7-1-ctl\n"

STUB_GH = """#!/bin/sh
case "$1 $2" in
  "cache list") cat "$STUB/keys" ;;
  "api --paginate") cat "$STUB/jobs" ;;
  *) echo "unexpected gh $*" >&2; exit 2 ;;
esac
"""
STUB_CURL = """#!/bin/sh
for a; do url=$a; done
id=${url%/logs}; id=${id##*/}
[ -f "$STUB/log-$id" ] || exit 22
cat "$STUB/log-$id"
"""


def run_check(tmp_path, *, keys=(PREFIX + "ctl",), probe_log=PROBE_LOG, control_log=CONTROL_LOG,
              probe="success", control="success", ctl_hit="true", old_hit="", new_hit=""):
    stub = tmp_path / "stub"
    stub.mkdir()
    for name, body in (("gh", STUB_GH), ("curl", STUB_CURL)):
        (stub / name).write_text(body)
        (stub / name).chmod(0o755)
    (stub / "keys").write_text("".join(k + "\n" for k in keys))
    (stub / "jobs").write_text("11 probe / probe\n22 control\n33 verify\n")
    if probe_log is not None:
        (stub / "log-11").write_text(probe_log)
    if control_log is not None:
        (stub / "log-22").write_text(control_log)
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", "STUB": str(stub),
           "GH_TOKEN": "t", "GH_REPO": "o/r", "RUN_ID": "7", "RUN_ATTEMPT": "1", "KEY_PREFIX": PREFIX,
           "GITHUB_API_URL": "https://api.github.com",
           "PROBE_RESULT": probe, "CONTROL_RESULT": control,
           "CTL_HIT": ctl_hit, "OLD_HIT": old_hit, "NEW_HIT": new_hit}
    script = lift_step(WORKFLOWS / CANARY, "      - name: Check")
    return subprocess.run([*STEP_BASH, script], env=env, text=True, capture_output=True, check=False)


def test_canary_check_is_green_when_saves_are_skipped_and_refused(tmp_path):
    r = run_check(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "canary green" in r.stdout


@pytest.mark.parametrize("case, kwargs, error", [
    ("a refused save left an entry", {"keys": (PREFIX + "ctl", PREFIX + "old")}, "entries under"),
    ("a skipped save left an entry", {"keys": (PREFIX + "ctl", PREFIX + "new")}, "entries under"),
    ("the control saved nothing", {"keys": ()}, "entries under"),
    ("the control did not restore", {"ctl_hit": "false"}, "control entry did not restore"),
    ("the refused save restored", {"old_hit": "true"}, "mode-ignoring client's save restored"),
    ("the skipped save restored", {"new_hit": "true"}, "mode-aware client's save restored"),
    ("the probe failed", {"probe": "failure"}, "probe job concluded 'failure'"),
    ("the control was skipped", {"control": "skipped"}, "control job concluded 'skipped'"),
    ("the probe ran with write", {"probe_log": PROBE_LOG.replace("Cache mode: read", "Cache mode: write")},
     "did not log 'Cache mode: read'"),
    ("the control ran read-only", {"control_log": CONTROL_LOG.replace("write", "read")},
     "did not log 'Cache mode: write'"),
    ("no client skip", {"probe_log": PROBE_LOG.replace("Cache save skipped", "Cache saved")},
     "did not log its skip"),
    ("no service refusal", {"probe_log": PROBE_LOG.replace("cache write denied:", "saved")},
     "service's refusal"),
    ("the probe log is unreadable", {"probe_log": None}, "cannot read the probe job's log"),
])
def test_canary_check_is_red(tmp_path, case, kwargs, error):
    r = run_check(tmp_path, **kwargs)
    assert r.returncode == 1, f"{case}: {r.stdout}{r.stderr}"
    assert "::error::" in r.stdout and error in r.stdout, f"{case}: {r.stdout}{r.stderr}"
    assert "canary green" not in r.stdout
