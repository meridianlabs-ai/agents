"""Tests for claude-auto.yml's gate and the land job's attempt refund.

The CI-fix loop's gate decides, in shell, whether the agent runs and on
which PR — so its `run:` scripts (`Resolve PR and check the auto label`,
`Gate and count`, and the land job's `Refund infra-crashed attempt`), and
the escalation's reset (the `reset-auto-counters` composite's step, given
the gate's `cid` as `comment-id`), are lifted out of the workflow and the
action the way the composer tests lift theirs and run here against a stub `gh`,
one case per rule from the 2026-09-04 Claude Security scan:

- 4122320: the PR is `inputs.pr_number`, viewed by number and required to
  be open, same-repo and on the run's head branch; a PR is never resolved
  from the branch name (`gh pr list --head` matches any head repository).
- 4121987: the attempt counter is read only from a marker comment whose
  author is one of `TRUSTED_LOGINS` (preferred) or holds write access;
  permission lookups are cached per login and fail closed, `[bot]` logins
  are never looked up, the count is parsed strictly, and the refund and
  the escalation's reset PATCH only that comment.

`verify-auto-labeler`'s `trusted-logins` input is covered the same way.
"""

import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "claude-auto.yml"
LABELER = ROOT / ".github" / "actions" / "verify-auto-labeler" / "action.yml"
RESET_ACTION = ROOT / ".github" / "actions" / "reset-auto-counters" / "action.yml"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_helpers import sh  # noqa: E402
from test_review_fix_gate import MARVIN, MARVIN_BOT, workflow_env  # noqa: E402

TRUSTED_LOGINS = workflow_env(WORKFLOW, "TRUSTED_LOGINS")

MARKER = "<!-- auto-fix-attempts -->"


def step_script(path: Path, anchor: str, indent: int) -> str:
    """The step's bash, lifted by text (PyYAML is not a test dependency): the
    block scalar under the first `run: |` after `anchor` is every following
    line indented by at least `indent` spaces, up to the first that is not."""
    lines = path.read_text().splitlines()
    start = lines.index(anchor)
    run_at = next(i for i in range(start, len(lines)) if lines[i] == " " * (indent - 2) + "run: |")
    body = []
    for line in lines[run_at + 1:]:
        if line.strip() == "":
            body.append("")
        elif line.startswith(" " * indent):
            body.append(line[indent:])
        else:
            break
    return "\n".join(body) + "\n"


RESOLVE = step_script(WORKFLOW, "        id: resolve", 10)
GATE = step_script(WORKFLOW, "        id: gate", 10)
RESET = step_script(RESET_ACTION, "    - id: reset", 8)
REFUND = step_script(WORKFLOW, "      - name: Refund infra-crashed attempt", 10)
VERIFY = step_script(LABELER, "    - id: verify", 8)

# A stub `gh` answering from fixture files in $STUB and appending every call
# to $STUB/calls. `pr list` is a hard failure: the gate must never list PRs
# by branch name. A missing permission fixture is gh's 404 (a deleted account
# or an unavailable API); a missing `pr` fixture is a PR that does not exist;
# a `patch-fail` fixture makes every PATCH fail.
GH_STUB = r'''#!/bin/bash
printf '%s\n' "$*" >>"$STUB/calls"
case "$1 $2" in
  "pr view")
    if [ -f "$STUB/pr" ]; then cat "$STUB/pr"; else echo '{"message":"Not Found"}'; exit 1; fi ;;
  "pr list") echo "gh pr list must not be called: $*" >&2; exit 3 ;;
  "pr edit"|"pr comment") exit 0 ;;
  "api repos/o/r/issues/"*)
    case "$2" in
      */comments\?per_page=100|*/comments) cat "$STUB/comments" ;;
      */timeline\?per_page=100) cat "$STUB/timeline" ;;
      *) echo "unexpected gh $*" >&2; exit 2 ;;
    esac ;;
  "api repos/o/r/collaborators/"*)
    login=${2#repos/o/r/collaborators/}; login=${login%/permission}
    if [ -f "$STUB/perm.$login" ]; then cat "$STUB/perm.$login"; else echo '{"message":"Not Found"}'; exit 1; fi ;;
  "api -X")
    case "$3 $4" in
      PATCH\ repos/o/r/issues/comments/*)
        if [ -f "$STUB/patch-fail" ]; then echo '{"message":"Server Error"}'; exit 1; fi
        printf '%s' "${6#body=}" >"$STUB/patched.${4##*/}" ;;
      POST\ repos/o/r/issues/*/comments) printf '%s' "${6#body=}" >"$STUB/posted" ;;
      *) echo "unexpected gh $*" >&2; exit 2 ;;
    esac ;;
  *) echo "unexpected gh $*" >&2; exit 2 ;;
esac
'''


def run_step(script: str, tmp_path: Path, env: dict, fixtures: dict, *, composite: bool = False):
    """Run a lifted script under the runner's shell options — `bash -e {0}`
    for a workflow `run:` step, `bash --noprofile --norc -eo pipefail {0}`
    for a composite's `shell: bash` step — so a bare non-zero status fails
    here as it would there."""
    binp = tmp_path / "bin"
    binp.mkdir(exist_ok=True)
    for name, body in (("gh", GH_STUB), ("sleep", "#!/bin/bash\nexit 0\n")):
        f = binp / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    for name, content in fixtures.items():
        (stub / name).write_text(content)
    out = tmp_path / "output"
    out.write_text("")
    e = {"PATH": f"{binp}:{os.environ['PATH']}", "STUB": str(stub), "GITHUB_OUTPUT": str(out),
         "REPO": "o/r", "GH_TOKEN": "x", "TRUSTED_LOGINS": TRUSTED_LOGINS, **env}
    opts = ["-e", "-o", "pipefail"] if composite else ["-e"]
    res = sh("bash", "--noprofile", "--norc", *opts, "-c", script, check=False, env=e)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    calls = (stub / "calls").read_text().splitlines() if (stub / "calls").exists() else []
    return res, outputs, calls, stub


def lookups(calls, login=None):
    return [c for c in calls if c.startswith("api repos/o/r/collaborators/")
            and (login is None or f"/{login}/" in c)]


def comment(cid, login, body, type_="User"):
    return {"id": cid, "user": {"login": login, "type": type_}, "body": body}


def counter(cid, login, n, **kw):
    return comment(cid, login, f"{MARKER}\n🤖 auto CI-fix attempts: {n} (cap 3).", **kw)


def pr_json(*, state="OPEN", cross=False, head="claude/issue-1-fix", labels=("auto",), number=7):
    return json.dumps({"number": number, "state": state, "isCrossRepository": cross,
                       "headRefName": head, "labels": [{"name": l} for l in labels]})


# --- Resolve PR and check the auto label (4122320) --------------------------


def resolve(tmp_path, *, pr_number="7", head="claude/issue-1-fix", pr=None, bind_ok="1", bind_reason=""):
    # PR_NUMBER is the bind step's BOUND PR (finding 4628657; the binding
    # itself is tested in test_ci_fix_binding.py), empty when it refused.
    env = {"HEAD": head, "PR_NUMBER": pr_number, "AUTO_LABEL": "auto",
           "BIND_OK": bind_ok, "BIND_REASON": bind_reason}
    fixtures = {"pr": pr} if pr is not None else {}
    return run_step(RESOLVE, tmp_path, env, fixtures)


def test_resolve_views_the_run_pr_by_number_and_never_lists_by_branch(tmp_path):
    res, out, calls, _ = resolve(tmp_path, pr=pr_json(labels=("auto", "engine:codex")))
    assert res.returncode == 0, res.stderr
    assert out == {"pr": "7", "engine": "codex", "act": "verify"}
    assert calls and calls[0].startswith("pr view 7 --repo o/r --json ")
    assert not any(c.startswith("pr list") for c in calls)
    code = "\n".join(l for l in RESOLVE.splitlines() if not l.lstrip().startswith("#"))
    assert "pr list" not in code and "--head" not in code


def test_resolve_skips_an_unbound_run_with_its_reason_and_reads_nothing(tmp_path):
    # The bind step refused (several PRs share the branch, a foreign or
    # stale run, a caller number that disagrees): no PR is viewed, no
    # counter is touched, and the reason lands in the log.
    reason = "run 9002 cannot be bound to one PR: #7 (open, base main) and #8 (open, base alt) all had head branch 'claude/issue-1-fix'"
    res, out, calls, _ = resolve(tmp_path, pr_number="", bind_ok="", bind_reason=reason, pr=pr_json())
    assert res.returncode == 0, res.stderr
    assert out == {"act": "skip"} and calls == []
    assert reason in res.stdout
    # A bound number without ok=1 is still a refusal (belt and braces).
    res, out, calls, _ = resolve(tmp_path, pr_number="7", bind_ok="", pr=pr_json())
    assert res.returncode == 0 and out == {"act": "skip"} and calls == []
    assert "could not be bound to a PR (no reason recorded)" in res.stdout


def test_resolve_unlabeled_pr_skips_but_still_names_it(tmp_path):
    res, out, _, _ = resolve(tmp_path, pr=pr_json(labels=()))
    assert res.returncode == 0, res.stderr
    assert out == {"pr": "7", "engine": "claude", "act": "skip"}
    assert "not @auto-labeled" in res.stdout


def test_resolve_skips_without_a_pr_number_and_calls_nothing(tmp_path):
    res, out, calls, _ = resolve(tmp_path, pr_number="", pr=pr_json())
    assert res.returncode == 0, res.stderr
    assert out == {"act": "skip"} and calls == []
    assert "No PR number from the triggering run" in res.stdout


def test_resolve_refuses_a_non_numeric_pr_number(tmp_path):
    res, out, calls, _ = resolve(tmp_path, pr_number="claude/issue-1-fix", pr=pr_json())
    assert res.returncode == 0, res.stderr
    assert out == {"act": "skip"} and calls == []


def test_resolve_skips_a_closed_pr(tmp_path):
    res, out, _, _ = resolve(tmp_path, pr=pr_json(state="MERGED"))
    assert res.returncode == 0, res.stderr
    assert out == {"act": "skip"} and "is MERGED, not open" in res.stdout


def test_resolve_refuses_a_fork_head(tmp_path):
    # The scan's scenario: the run's PR list names a fork PR whose branch is
    # named like the same-repo branch. It is never the PR the loop acts on.
    res, out, _, _ = resolve(tmp_path, pr=pr_json(cross=True))
    assert res.returncode == 0, res.stderr
    assert out == {"act": "skip"} and "head is in a fork" in res.stdout


def test_resolve_refuses_a_pr_on_another_branch(tmp_path):
    res, out, _, _ = resolve(tmp_path, pr=pr_json(head="other-branch"))
    assert res.returncode == 0, res.stderr
    assert out == {"act": "skip"}
    assert "head branch is 'other-branch' but the failed CI run's is 'claude/issue-1-fix'" in res.stdout


# --- Gate and count (4121987) ------------------------------------------------


def gate(tmp_path, comments, *, cap="3", trusted="i-am-marvin", perms=None):
    env = {"PR": "7", "ENGINE": "claude", "RESOLVED": "verify", "LABELER_VERDICT": "ok",
           "CAP": cap, "MARKER": MARKER, "TRUSTED_LOGINS": trusted}
    fixtures = {"comments": comments if isinstance(comments, str) else json.dumps(comments)}
    for login, perm in (perms or {}).items():
        fixtures[f"perm.{login}"] = perm
    return run_step(GATE, tmp_path, env, fixtures)


def test_gate_ignores_an_outsiders_newer_marker_and_counts_from_marvins(tmp_path):
    # The scan's attack: an outsider posts the marker with `attempts: 99`
    # (to force an escalation) after marvin's real counter of 1.
    res, out, calls, _ = gate(tmp_path, [counter(10, "i-am-marvin", 1), counter(11, "outsider", 99)],
                              perms={"outsider": "read"})
    assert res.returncode == 0, res.stderr
    assert out["act"] == "fix" and out["attempt"] == "2" and out["cid"] == "10"
    assert "Counter: comment 10 by i-am-marvin reads attempts=1." in res.stdout
    assert "Ignored marker comment 11 by outsider" in res.stdout
    # The loop's own comment exists, so nobody's permission was looked up.
    assert lookups(calls) == []


def test_gate_ignores_an_outsiders_reset_when_it_is_the_only_marker(tmp_path):
    # `attempts: 0` from a no-role account is not a counter: the gate counts
    # from 0 on its own terms and does NOT adopt the comment (cid empty, so
    # Record attempt posts a fresh one instead of PATCHing the outsider's).
    res, out, calls, _ = gate(tmp_path, [counter(11, "outsider", 0)], perms={"outsider": "read"})
    assert res.returncode == 0, res.stderr
    assert out["act"] == "fix" and out["attempt"] == "1" and out["cid"] == ""
    assert "No counter comment from a trusted author" in res.stdout
    assert len(lookups(calls, "outsider")) == 1


def test_gate_counts_a_write_access_humans_comment_when_the_loop_has_none(tmp_path):
    res, out, calls, _ = gate(tmp_path, [counter(12, "alice", 2)], perms={"alice": "write"})
    assert res.returncode == 0, res.stderr
    assert out["attempt"] == "3" and out["cid"] == "12"
    assert len(lookups(calls, "alice")) == 1


def test_gate_accepts_admin_and_maintain_too(tmp_path):
    for perm in ("admin", "maintain"):
        res, out, _, _ = gate(tmp_path, [counter(12, "alice", 2)], perms={"alice": perm})
        assert res.returncode == 0, res.stderr
        assert out["cid"] == "12", perm


def test_gate_prefers_the_loops_own_comment_over_a_newer_maintainers(tmp_path):
    res, out, calls, _ = gate(tmp_path, [counter(10, "i-am-marvin", 1), counter(12, "alice", 2)],
                              perms={"alice": "write"})
    assert res.returncode == 0, res.stderr
    assert out["cid"] == "10" and out["attempt"] == "2"
    assert lookups(calls) == []


def test_gate_takes_the_newest_of_the_loops_own_comments(tmp_path):
    res, out, _, _ = gate(tmp_path, [counter(10, "i-am-marvin", 1), comment(11, "bob", "hi"),
                                     counter(13, "i-am-marvin", 2)])
    assert res.returncode == 0, res.stderr
    assert out["cid"] == "13" and out["attempt"] == "3"


def test_gate_treats_a_failed_permission_lookup_as_untrusted(tmp_path):
    # No perm fixture: gh 404s through every retry (sleep is stubbed).
    res, out, calls, _ = gate(tmp_path, [counter(12, "ghost", 2)])
    assert res.returncode == 0, res.stderr
    assert out["attempt"] == "1" and out["cid"] == ""
    assert len(lookups(calls, "ghost")) == 4  # ghr's four attempts


def test_gate_looks_each_login_up_once(tmp_path):
    res, out, calls, _ = gate(tmp_path, [counter(11, "bob", 5), counter(12, "bob", 6), counter(13, "bob", 7)],
                              perms={"bob": "read"})
    assert res.returncode == 0, res.stderr
    assert out["cid"] == "" and len(lookups(calls, "bob")) == 1


def test_gate_never_trusts_or_looks_up_a_bot_login(tmp_path):
    # github-actions[bot] is any repository's workflow; even a permission
    # fixture claiming admin must not be consulted.
    res, out, calls, _ = gate(tmp_path, [counter(11, "github-actions[bot]", 0, type_="Bot")],
                              perms={"github-actions[bot]": "admin"})
    assert res.returncode == 0, res.stderr
    assert out["cid"] == "" and lookups(calls) == []


def test_gate_trusted_logins_is_a_list_and_ignores_spaces(tmp_path):
    # Phase 2 shape: the App's bot login is trusted because it is LISTED,
    # not because of the `[bot]` suffix rule (which refuses unlisted bots).
    res, out, calls, _ = gate(tmp_path, [counter(11, "marvin-app[bot]", 1, type_="Bot")],
                              trusted="i-am-marvin, marvin-app[bot]")
    assert res.returncode == 0, res.stderr
    assert out["cid"] == "11" and out["attempt"] == "2" and lookups(calls) == []


def test_gate_parses_the_count_strictly(tmp_path):
    doubled = comment(10, "i-am-marvin", f"{MARKER}\nattempts: 2\nattempts: 7")
    res, out, _, _ = gate(tmp_path, [doubled])
    assert res.returncode == 0, res.stderr
    assert out["attempt"] == "1" and out["cid"] == "10"
    assert "carries no single valid 'attempts: N'" in res.stdout
    junk = comment(10, "i-am-marvin", f"{MARKER}\nattempts: many")
    res, out, _, _ = gate(tmp_path, [junk])
    assert res.returncode == 0 and out["attempt"] == "1"


def test_gate_rejects_a_count_token_that_is_not_one_to_nine_digits(tmp_path):
    # Review round 1: `3junk` used to read as 3 (the sed kept the digits and
    # dropped the rest) and a 19-digit count wrapped `next` negative.
    for bad in ("3junk", "9223372036854775807", "1234567890", "3.", "-1", "0x10"):
        res, out, _, _ = gate(tmp_path, [comment(10, "i-am-marvin", f"{MARKER}\nattempts: {bad} (cap 3).")], cap="3")
        assert res.returncode == 0, (bad, res.stderr)
        assert out["attempt"] == "1" and out["act"] == "fix", bad
        assert "no single valid 'attempts: N'" in res.stdout, bad


def test_gate_reads_a_leading_zero_as_decimal(tmp_path):
    # `08` is octal to bash arithmetic and aborted the gate under set -e.
    res, out, _, _ = gate(tmp_path, [comment(10, "i-am-marvin", f"{MARKER}\nattempts: 08 (cap 3).")], cap="3")
    assert res.returncode == 0, res.stderr
    assert out["attempt"] == "9" and out["act"] == "escalate"
    res, out, _, _ = gate(tmp_path, [comment(10, "i-am-marvin", f"{MARKER}\nattempts: 000000002 (cap 3).")], cap="3")
    assert res.returncode == 0 and out["attempt"] == "3" and out["act"] == "fix"


def test_gate_reads_a_reset_body_as_zero(tmp_path):
    reset = comment(10, "i-am-marvin", f"{MARKER}\n🤖 auto CI-fix attempts reset (on escalation) — the next attempt starts at 1 with the full cap.")
    res, out, _, _ = gate(tmp_path, [reset])
    assert res.returncode == 0, res.stderr
    assert out["attempt"] == "1" and out["cid"] == "10" and out["act"] == "fix"


def test_gate_escalates_past_the_cap_only_on_a_trusted_count(tmp_path):
    res, out, _, _ = gate(tmp_path, [counter(10, "i-am-marvin", 3)], cap="3")
    assert res.returncode == 0 and out["act"] == "escalate" and out["attempt"] == "4"
    # An outsider's 999 cannot force it.
    res, out, _, _ = gate(tmp_path, [counter(10, "i-am-marvin", 1), counter(11, "outsider", 999)],
                          cap="3", perms={"outsider": "read"})
    assert res.returncode == 0 and out["act"] == "fix" and out["attempt"] == "2"


def test_gate_flattens_paginated_pages(tmp_path):
    pages = json.dumps([comment(1, "x", "first page")]) + "\n" + json.dumps([counter(10, "i-am-marvin", 2)])
    res, out, _, _ = gate(tmp_path, pages)
    assert res.returncode == 0, res.stderr
    assert out["cid"] == "10" and out["attempt"] == "3"


def test_gate_skips_before_reading_comments_when_unresolved_or_unverified(tmp_path):
    for env in ({"RESOLVED": "skip"}, {"LABELER_VERDICT": "disarmed"}):
        e = {"PR": "7", "ENGINE": "claude", "RESOLVED": "verify", "LABELER_VERDICT": "ok",
             "CAP": "3", "MARKER": MARKER, **env}
        res, out, calls, _ = run_step(GATE, tmp_path, e, {"comments": "[]"})
        assert res.returncode == 0 and out["act"] == "skip" and calls == []


# --- Refund infra-crashed attempt (4121987, land side) ----------------------


def refund(tmp_path, comments, *, attempt="2", perms=None):
    env = {"PR": "7", "ATTEMPT": attempt, "CAP": "3", "MARKER": MARKER}
    fixtures = {"comments": json.dumps(comments)}
    for login, perm in (perms or {}).items():
        fixtures[f"perm.{login}"] = perm
    return run_step(REFUND, tmp_path, env, fixtures)


def test_refund_patches_only_the_loops_own_comment(tmp_path):
    res, _, _, stub = refund(tmp_path, [counter(10, "i-am-marvin", 2), counter(11, "outsider", 99)],
                             perms={"outsider": "read"})
    assert res.returncode == 0, res.stderr
    assert (stub / "patched.10").exists() and not (stub / "patched.11").exists()
    assert "attempts: 1 (cap 3)" in (stub / "patched.10").read_text()
    assert "Refunded attempt: counter 2 -> 1" in res.stdout


def test_refund_does_nothing_without_a_trusted_counter(tmp_path):
    res, _, _, stub = refund(tmp_path, [counter(11, "outsider", 99)], perms={"outsider": "read"})
    assert res.returncode == 0, res.stderr
    assert not list(stub.glob("patched.*"))
    assert "nothing to refund" in res.stdout


def test_refund_reads_an_unparsable_or_reset_body_as_zero(tmp_path):
    # As in the gate. The old fallback to this run's ATTEMPT would have undone
    # an escalation's reset that landed between this run's gate and its refund.
    reset_body = comment(10, "i-am-marvin", f"{MARKER}\n🤖 auto CI-fix attempts reset (on escalation) — the next attempt starts at 1 with the full cap.")
    res, _, _, stub = refund(tmp_path, [reset_body], attempt="3")
    assert res.returncode == 0, res.stderr
    assert "attempts: 0 (cap 3)" in (stub / "patched.10").read_text()
    assert "treating the count as 0" in res.stdout
    doubled = comment(10, "i-am-marvin", f"{MARKER}\nattempts: 2\nattempts: 7")
    res, _, _, stub = refund(tmp_path, [doubled], attempt="3")
    assert res.returncode == 0 and "attempts: 0 (cap 3)" in (stub / "patched.10").read_text()
    octal = comment(10, "i-am-marvin", f"{MARKER}\nattempts: 08 (cap 3).")
    res, _, _, stub = refund(tmp_path, [octal], attempt="3")
    assert res.returncode == 0 and "attempts: 7 (cap 3)" in (stub / "patched.10").read_text()


def test_a_refunded_attempt_is_counted_from_where_the_refund_left_it_and_the_cap_holds(tmp_path):
    # gate (attempt 1 recorded) → refund → gate: the next round is attempt 1
    # again — the CI-fix loop has no head marker, so its only bound is the
    # cap, and a refund never takes the count below 0. From the cap the
    # refund gives one attempt back and the gate escalates on the one after.
    res, _, _, stub = refund(tmp_path, [counter(10, "i-am-marvin", 1)], attempt="1")
    assert res.returncode == 0 and "attempts: 0 (cap 3)" in (stub / "patched.10").read_text()
    res, out, _, _ = gate(tmp_path, [comment(10, "i-am-marvin", (stub / "patched.10").read_text())])
    assert res.returncode == 0 and out["act"] == "fix" and out["attempt"] == "1"
    res, _, _, stub = refund(tmp_path, [counter(10, "i-am-marvin", 3)], attempt="3")
    assert "attempts: 2 (cap 3)" in (stub / "patched.10").read_text()
    res, out, _, _ = gate(tmp_path, [comment(10, "i-am-marvin", (stub / "patched.10").read_text())])
    assert out["act"] == "fix" and out["attempt"] == "3"
    res, out, _, _ = gate(tmp_path, [counter(10, "i-am-marvin", 3)])
    assert out["act"] == "escalate" and out["attempt"] == "4"


def test_the_refund_fires_only_on_a_step_the_runner_never_entered():
    """The refund's gating inputs, not only its comment selection (Claude
    Security 4628735): the fix job's result being `cancelled` (the server's
    fact; needs actions:write, which no token in the fix job holds) or its
    `agent_skipped` output — both engines' agent steps `skipped`, a step
    outcome the runner settled before any agent code ran — plus nothing
    pushed. Never `agent_outcome`, which the agent decides by how it ends its
    own step, and never an execution file. The Land step admits a bundle-less
    hand-back only on the agent step's success, the direction the agent
    cannot push, so a refunded round never posts the `@review` that would
    re-arm the loop."""
    text = WORKFLOW.read_text()
    assert "id: launched" not in text and "agent_started" not in text
    fix = text[text.index("  fix:\n"):text.index("  land:\n")]
    outputs = fix[fix.index("    outputs:\n"):fix.index("    steps:\n")]
    assert "agent_skipped: ${{ steps.claude.outcome == 'skipped' && steps.codexfix.outcome == 'skipped' && 'true' || 'false' }}" in outputs
    assert ("agent_outcome: ${{ (steps.codexprep.outcome == 'failure' || steps.codexuser.outcome == 'failure' || "
            "steps.codexfix.outcome == 'failure') && 'failure' || (steps.codexfix.outcome == 'success' && 'success') || "
            "steps.claude.outcome }}") in outputs
    refund_step = text[text.index("      - name: Refund infra-crashed attempt"):]
    refund_step = refund_step[:refund_step.index("        run: |")]
    condition = " ".join(refund_step[refund_step.index("if: >-") + len("if: >-"):refund_step.index("env:")].split())
    assert condition == ("always() && needs.gate.outputs.act == 'fix' && "
                         "(needs.fix.result == 'cancelled' || needs.fix.outputs.agent_skipped == 'true') && "
                         "steps.land.outputs.pushed != '1'")
    assert "agent_outcome" not in condition and "execution" not in refund_step
    land = text[text.index("      - name: Land\n"):text.index("      # The revalidation refused")]
    assert "allow-no-change-handback: ${{ needs.fix.outputs.agent_outcome == 'success' && 'true' || 'false' }}" in land


# --- Reset the attempt counter (escalation) -----------------------------------


def reset(tmp_path, cid, fixtures=None):
    # The composite as claude-auto.yml calls it: the gate's cid as comment-id,
    # TRUSTED_LOGINS (run_step's) as the lookup filter for an empty one.
    env = {"PR": "7", "COMMENT_ID": cid, "COUNTERS": "attempts",
           "REASON": "on escalation — re-adding `auto` starts a fresh budget"}
    return run_step(RESET, tmp_path, env, fixtures or {}, composite=True)


def test_escalation_resets_the_comment_the_gate_counted_from_and_the_gate_restarts(tmp_path):
    # Review round 1: marvin's counter (10) is at the cap and an outsider's
    # newer marker (11) exists. The gate escalates from 10, so the reset must
    # rewrite 10 — the composite's lookup path would have rewritten the
    # newest marker, 11, leaving 10 exhausted so that re-adding the label
    # escalated again on sight; hence the gate's cid travels as comment-id.
    marvin, outsider = counter(10, "i-am-marvin", 3), counter(11, "outsider", 99)
    res, out, _, _ = gate(tmp_path, [marvin, outsider], cap="3", perms={"outsider": "read"})
    assert res.returncode == 0, res.stderr
    assert out["act"] == "escalate" and out["cid"] == "10"
    res, rout, _, stub = reset(tmp_path, out["cid"])
    assert res.returncode == 0, res.stderr
    assert rout == {"ok": "1"}
    assert (stub / "patched.10").exists() and not (stub / "patched.11").exists()
    marvin["body"] = (stub / "patched.10").read_text()
    assert marvin["body"].startswith(MARKER + "\n") and "attempts:" not in marvin["body"]
    # Re-armed: the next red CI counts from the reset comment, not the outsider's.
    res, out, _, _ = gate(tmp_path, [marvin, outsider], cap="3", perms={"outsider": "read"})
    assert res.returncode == 0, res.stderr
    assert out["act"] == "fix" and out["attempt"] == "1" and out["cid"] == "10"


def reengage(tmp_path, comments, perms=None):
    # The composite as claude.yml's re-engagement calls it: both counters,
    # no gate and so no comment-id, TRUSTED_LOGINS (run_step's).
    env = {"PR": "7", "COMMENT_ID": "", "COUNTERS": "rounds attempts", "REASON": "on re-engagement — fresh cap"}
    fixtures = {"comments": json.dumps(comments)}
    for login, perm in (perms or {}).items():
        fixtures[f"perm.{login}"] = perm
    return run_step(RESET, tmp_path, env, fixtures, composite=True)


def test_reengagement_resets_a_maintainers_counter_the_gate_counts_from(tmp_path):
    # Alice (write access) hand-wrote the counter and it sits at the cap; the
    # gate counts from it, so re-engagement must reset THAT comment — a
    # lookup that knew only the loop's own logins found nothing, returned
    # ok=1, and the next red CI escalated again at attempt 4.
    alice = counter(12, "alice", 3)
    res, out, gate_calls, _ = gate(tmp_path, [alice], cap="3", perms={"alice": "write"})
    assert res.returncode == 0, res.stderr
    assert out["act"] == "escalate" and out["cid"] == "12"
    res, rout, calls, stub = reengage(tmp_path, [alice], perms={"alice": "write"})
    assert res.returncode == 0, res.stderr
    assert rout == {"ok": "1"}
    assert (stub / "patched.12").exists()
    # The stub's call log spans both runs: the reset itself looked alice up once.
    assert len(lookups(calls, "alice")) - len(lookups(gate_calls, "alice")) == 1
    alice["body"] = (stub / "patched.12").read_text()
    assert "attempts:" not in alice["body"]
    res, out, _, _ = gate(tmp_path, [alice], cap="3", perms={"alice": "write"})
    assert res.returncode == 0, res.stderr
    assert out["act"] == "fix" and out["attempt"] == "1" and out["cid"] == "12"


def test_reengagement_prefers_the_loops_own_counter_and_ignores_outsiders_and_bots(tmp_path):
    marvin, alice = counter(10, "i-am-marvin", 3), counter(12, "alice", 3)
    outsider, bot = counter(13, "outsider", 99), counter(14, "some-app[bot]", 99, type_="Bot")
    # The loop's own comment wins over a newer maintainer's, with no lookup —
    # the gate's precedence.
    res, out, calls, stub = reengage(tmp_path, [marvin, alice], perms={"alice": "write"})
    assert res.returncode == 0 and out == {"ok": "1"}, res.stderr
    assert (stub / "patched.10").exists() and not (stub / "patched.12").exists() and lookups(calls) == []
    # Outsiders and Apps are never the counter: nothing to reset, the App never looked up.
    res, out, calls, stub = reengage(tmp_path, [outsider, bot], perms={"outsider": "read"})
    assert res.returncode == 0 and out == {"ok": "1"}, res.stderr
    assert not (stub / "patched.13").exists() and not (stub / "patched.14").exists()
    assert lookups(calls, "some-app[bot]") == [] and len(lookups(calls, "outsider")) == 1
    assert "nothing to reset" in res.stdout


def test_reset_without_a_trusted_counter_has_nothing_to_do(tmp_path):
    # No cid from the gate: the composite's lookup runs — the loop's own
    # marker first, else a VERIFIED write-access author's — so an outsider's
    # marker (permission read, no write) is not a counter to reset.
    res, out, _, stub = reset(tmp_path, "", {"comments": json.dumps([counter(11, "outsider", 99)]),
                                             "perm.outsider": "read"})
    assert res.returncode == 0, res.stderr
    assert out == {"ok": "1"} and not list(stub.glob("patched.*"))
    assert "nothing to reset" in res.stdout


def test_reengagement_passes_an_outsiders_newer_marker_to_reach_a_maintainers_counter(tmp_path):
    # Review round 7: `shell: bash` runs the composite under -e, and a bare
    # `write_access; rc=$?` ended the step on the outsider's verified
    # non-write (status 1) before `ok` was written — the re-engagement died
    # and Alice's exhausted counter stayed. The status is captured, so the
    # walk continues to her comment.
    alice, outsider = counter(12, "alice", 3), counter(13, "outsider", 99)
    res, out, calls, stub = reengage(tmp_path, [alice, outsider], perms={"alice": "write", "outsider": "read"})
    assert res.returncode == 0, res.stderr
    assert out == {"ok": "1"}
    assert (stub / "patched.12").exists() and not (stub / "patched.13").exists()
    assert len(lookups(calls, "outsider")) == 1 and len(lookups(calls, "alice")) == 1


def test_reset_with_an_unverifiable_marker_author_is_unresolved_not_nothing(tmp_path):
    # The permission lookup fails after retries (no fixture: gh's 404 or an
    # unavailable API). "Nothing to reset, ok=1" would leave Alice's
    # exhausted counter in place for the next gate to escalate on once
    # permissions read again; the composite says it could not decide.
    res, out, calls, stub = reengage(tmp_path, [counter(12, "alice", 3)])
    assert res.returncode == 0, res.stderr
    assert out == {"ok": "0"} and not list(stub.glob("patched.*"))
    assert len(lookups(calls, "alice")) == 4, "retried, then unresolved"
    assert "::warning::reset-auto-counters: could not verify" in res.stdout


def test_reset_reports_a_patch_that_fails_after_retries(tmp_path):
    res, out, calls, _ = reset(tmp_path, "10", {"patch-fail": ""})
    assert res.returncode == 0, res.stderr
    assert out == {"ok": "0"}
    assert len([c for c in calls if c.startswith("api -X PATCH")]) == 4
    assert "could not reset" in res.stdout


# --- verify-auto-labeler's trusted-logins input ------------------------------


def verify(tmp_path, labeler, *, trusted, perms=None):
    env = {"PR": "7", "AUTO_LABEL": "auto", "TRUSTED_LOGINS": trusted}
    timeline = json.dumps([{"event": "labeled", "label": {"name": "auto"}, "actor": {"login": labeler}}])
    fixtures = {"timeline": timeline, "comments": "[]"}
    for login, perm in (perms or {}).items():
        fixtures[f"perm.{login}"] = perm
    return run_step(VERIFY, tmp_path, env, fixtures, composite=True)


def test_labeler_authorizes_another_authors_pr_on_a_write_access_humans_label(tmp_path):
    # The trusted-labeler policy, preserved by finding 4628657's fix: a
    # maintainer's `auto` label authorizes the round whoever authored the PR
    # (the author is never consulted — no lookup of any login but the
    # labeler's), and a read-only account's label disarms. Replacing this
    # with "the PR author must be a writer" would refuse deliberately
    # authorized work and would not tell two writers' PRs apart anyway.
    res, out, calls, _ = verify(tmp_path, "a-maintainer", trusted=TRUSTED_LOGINS, perms={"a-maintainer": "write"})
    assert res.returncode == 0, res.stderr
    assert out["verdict"] == "ok" and out["labeler"] == "a-maintainer"
    assert len(lookups(calls)) == 1 and lookups(calls) == lookups(calls, "a-maintainer")
    res, out, calls, _ = verify(tmp_path, "read-only-member", trusted=TRUSTED_LOGINS,
                                perms={"read-only-member": "read"})
    assert res.returncode == 0, res.stderr
    assert out["verdict"] == "disarmed"
    assert any(c.startswith("pr edit 7 --repo o/r --remove-label auto") for c in calls)


def test_labeler_trusts_a_listed_bot_login_without_a_lookup(tmp_path):
    res, out, calls, _ = verify(tmp_path, "marvin-app[bot]", trusted="i-am-marvin,marvin-app[bot]")
    assert res.returncode == 0, res.stderr
    assert out["verdict"] == "ok" and out["labeler"] == "marvin-app[bot]" and lookups(calls) == []


def test_labeler_reads_the_input_not_a_hardcoded_login(tmp_path):
    # With the machine account NOT listed, its label is judged like anyone's.
    res, out, calls, _ = verify(tmp_path, "i-am-marvin", trusted="marvin-app[bot]",
                                perms={"i-am-marvin": "read\t"})
    assert res.returncode == 0, res.stderr
    assert out["verdict"] == "disarmed" and len(lookups(calls, "i-am-marvin")) == 1
    assert any(c.startswith("pr edit 7 --repo o/r --remove-label auto") for c in calls)


def test_labeler_default_input_is_the_machine_account():
    # Both of its logins: the User (the PAT) and the Phase 2 GitHub App's.
    text = LABELER.read_text()
    assert "  trusted-logins:\n" in text and f"    default: {MARVIN},{MARVIN_BOT}\n" in text
    assert 'elif [ "$labeler" = "i-am-marvin" ]' not in text


def test_labeler_passes_the_machine_accounts_bot_login_by_name_and_refuses_other_apps(tmp_path):
    # Under the workflow's own TRUSTED_LOGINS: the App login the marvin app
    # labels with under Phase 2 passes before the App case (no lookup — the
    # endpoint cannot report an App's permission); any other App, including
    # github-actions[bot], is still `unverified` and never looked up.
    res, out, calls, _ = verify(tmp_path, MARVIN_BOT, trusted=TRUSTED_LOGINS)
    assert res.returncode == 0, res.stderr
    assert out["verdict"] == "ok" and out["labeler"] == MARVIN_BOT and lookups(calls) == []
    for app in ("github-actions[bot]", "foo[bot]"):
        res, out, calls, _ = verify(tmp_path, app, trusted=TRUSTED_LOGINS)
        assert res.returncode == 0, res.stderr
        assert out["verdict"] == "unverified" and lookups(calls) == [], app


# --- the one trusted value ----------------------------------------------------


def test_workflow_declares_trusted_logins_once_and_passes_it_to_the_composite():
    text = WORKFLOW.read_text()
    assert text.count("\nenv:\n") == 1 and TRUSTED_LOGINS == f"{MARVIN},{MARVIN_BOT}"
    assert "trusted-logins: ${{ env.TRUSTED_LOGINS }}" in text
    # No trust decision names the login itself: the remaining literals are
    # the env value and the commit identity (the cc-target exclusion reads
    # the env too).
    for script in (RESOLVE, GATE, RESET, REFUND):
        assert "i-am-marvin" not in script
    # The fix agents' bot allow-lists carry the value: workflow_run's actor
    # is the pusher, the App's bot login under Phase 2.
    assert "allowed_bots: ${{ env.TRUSTED_LOGINS }}" in text
    assert "allow-bot-users: ${{ format('claude,{0}', env.TRUSTED_LOGINS) }}" in text


def test_gate_counts_the_machine_accounts_bot_login_under_the_workflow_value_and_not_other_apps(tmp_path):
    # Phase 2: the gate records the counter as the App's bot login. Its newest
    # marker is the counter (before the `[bot]` refusal, no lookup); another
    # App's marker, even newer, is ignored and never looked up.
    res, out, calls, _ = gate(tmp_path, [counter(10, MARVIN, 1), counter(11, MARVIN_BOT, 2, type_="Bot"),
                                         counter(12, "foo[bot]", 99, type_="Bot")], trusted=TRUSTED_LOGINS)
    assert res.returncode == 0, res.stderr
    assert out["act"] == "fix" and out["attempt"] == "3" and out["cid"] == "11"
    assert lookups(calls) == []
