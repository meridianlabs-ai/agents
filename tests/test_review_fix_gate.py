"""Tests for claude-auto-review.yml's author filters (Claude Security finding
4121983).

The review-fix loop keeps its state in PR comments and finds it by marker
substring: the reviewer's verdict, the round counter with the previous head
SHA, the re-review requests the stale-verdict guard compares against, and
the converged hand-off's double-post marker. On a public caller any account
can post those substrings, so every read now also checks the author against
the workflow-level TRUSTED_LOGINS / REVIEWER_LOGINS lists. The three steps
that read them — the gate's `Gate and count`, the `Converged handoff`, and
the land job's `Refund infra-crashed round` — are lifted out of the workflow
and run here against a stub `gh` (one case per forgery, plus the positive
path each filter must keep working), the way test_review_fix_composer.py
runs the manifest composer.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_helpers import sh  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "claude-auto-review.yml"
RESET = ROOT / ".github" / "actions" / "reset-auto-counters" / "action.yml"

HEAD = "a" * 40
OLD = "b" * 40
MARKER = "<!-- auto-review-rounds -->"
T0, T1, T2, T3 = ("2026-09-15T10:00:00Z", "2026-09-15T11:00:00Z",
                  "2026-09-15T12:00:00Z", "2026-09-15T13:00:00Z")


def lift_step(workflow: Path, anchor: str) -> str:
    """A step's bash, lifted from the workflow by text (PyYAML is not a test
    dependency): the block scalar under the first `run: |` after the anchor
    line is every following line indented past the `run:` key, up to the
    first that is not."""
    lines = workflow.read_text().splitlines()
    start = lines.index(anchor)
    run_at = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run_at]) - len(lines[run_at].lstrip()) + 2
    body = []
    for line in lines[run_at + 1:]:
        if line.strip() == "":
            body.append("")
        elif line.startswith(" " * indent):
            body.append(line[indent:])
        else:
            break
    return "\n".join(body) + "\n"


def comment(cid, login, body, at):
    return {"id": cid, "user": {"login": login}, "body": body, "created_at": at}


def verdict(login, word, at, cid=1):
    return comment(cid, login, f"Review. <!-- claude-review-summary --><!-- claude-review-verdict:{word} -->", at)


def counter(login, rounds, at, cid, head=None):
    body = f"{MARKER}\n🤖 auto review rounds: {rounds} (cap 10)."
    if head:
        body += f"\n<!-- auto-review-head:{head} -->"
    return comment(cid, login, body, at)


# The stub `gh`: the comments list and the collaborators permission endpoint,
# from files under $STATE. A login missing from `perms` is an API failure
# (error body on stdout, non-zero — what `gh api` does on a 404). `sleep` is
# stubbed so the gate's retry helper spins instantly. Every lookup is logged
# so the tests can check the per-login cache and the no-lookup paths.
GH_STUB = r"""
sleep() { :; }
gh() {
  case "$*" in
    "api repos/o/r/issues/42/comments --paginate") cat "$STATE/comments.json" ;;
    "api repos/o/r/collaborators/"*"/permission --jq .permission")
      local login=${2#repos/o/r/collaborators/}; login=${login%/permission}
      echo "$login" >>"$STATE/lookups"
      local p; p=$(awk -v l="$login" '$1==l {print $2}' "$STATE/perms")
      [ -n "$p" ] || { echo '{"message":"Not Found"}'; return 1; }
      echo "$p" ;;
    *) echo "unexpected gh call: $*" >&2; return 1 ;;
  esac
}
"""


def outputs(path: Path) -> dict:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def lookups(state: Path) -> list:
    f = state / "lookups"
    return f.read_text().split() if f.exists() else []


def fresh_state(tmp_path: Path) -> Path:
    """$STATE for one lifted-step run: helpers may run twice in a test, so
    the files a previous run recorded (lookups, posts, PATCHes) go first."""
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    for f in state.iterdir():
        f.unlink()
    return state


def run_gate(tmp_path, comments, perms=None, *, env_extra=None):
    state = fresh_state(tmp_path)
    (state / "comments.json").write_text(json.dumps(comments))
    (state / "perms").write_text("".join(f"{k} {v}\n" for k, v in (perms or {}).items()))
    out = tmp_path / "out"
    out.write_text("")
    pr_json = {"state": "OPEN", "closedAt": None, "headRefName": "claude/issue-9-x",
               "headRefOid": HEAD, "isCrossRepository": False, "labels": [{"name": "auto"}]}
    env = {
        "GITHUB_OUTPUT": str(out), "STATE": str(state), "REPO": "o/r", "PR": "42",
        "PR_JSON": json.dumps(pr_json), "RESOLVED": "verify", "LABELER_VERDICT": "ok",
        "CAP": "10", "REVIEWER": "claude[bot]", "HANDOFF_MENTION": "someone",
        "ANCHOR_REPO": "", "MARKER": MARKER,
        "TRUSTED_LOGINS": "i-am-marvin", "REVIEWER_LOGINS": "i-am-marvin,claude[bot]",
    }
    env.update(env_extra or {})
    r = sh("bash", "-c", GH_STUB + lift_step(WORKFLOW, "        id: gate"), check=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return r, outputs(out), state


# --- the verdict ------------------------------------------------------------


def test_forged_clean_verdict_from_an_outsider_is_ignored(tmp_path):
    r, o, _ = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                  verdict("nobody", "clean", T2, cid=2)])
    assert o["verdict"] == "suggestions"
    assert o["act"] == "fix"


def test_reviewer_identities_clean_verdict_converges(tmp_path):
    for login in ("i-am-marvin", "claude[bot]"):
        _, o, _ = run_gate(tmp_path, [verdict(login, "clean", T1)])
        assert o["act"] == "converged", login


def test_callers_reviewer_login_override_is_a_verdict_author(tmp_path):
    _, o, _ = run_gate(tmp_path, [verdict("other-reviewer[bot]", "clean", T1)],
                       env_extra={"REVIEWER": "other-reviewer[bot]"})
    assert o["act"] == "converged"
    _, o, _ = run_gate(tmp_path, [verdict("other-reviewer[bot]", "clean", T1)])
    assert o["act"] == "fix", "not a reviewer identity for callers that keep the default"


# --- the round counter -------------------------------------------------------


def test_forged_counter_is_neither_adopted_nor_counted(tmp_path):
    # The loop's own counter says round 2 on an older tip; a newer forgery
    # claims round 999 (cap abuse) and the CURRENT tip (no-progress abuse).
    _, o, _ = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                  counter("i-am-marvin", 2, T0, cid=100, head=OLD),
                                  counter("nobody", 999, T2, cid=200, head=HEAD)])
    assert o["act"] == "fix"
    assert o["round"] == "3"
    assert o["cid"] == "100", "Record round must PATCH the loop's comment, never the forgery"
    assert o.get("stalled") is None


def test_outsider_counter_alone_counts_as_no_counter(tmp_path):
    _, o, _ = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                  counter("nobody", 999, T2, cid=200, head=HEAD)])
    assert o["act"] == "fix"
    assert o["round"] == "1"
    assert o["cid"] == "", "a fresh comment gets POSTed; the forgery is never PATCHed"


def test_malformed_counter_counts_from_zero_instead_of_failing_the_gate(tmp_path):
    body = f"{MARKER}\nrounds: 3\nrounds: 4\n<!-- auto-review-head:not-a-sha -->"
    r, o, _ = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                  comment(100, "i-am-marvin", body, T0)])
    assert o["act"] == "fix"
    assert o["round"] == "1"
    assert o["cid"] == "100"
    assert "counting from 0" in r.stdout


@pytest.mark.parametrize("value", ["10oops", "9223372036854775807", "3\\nrounds: 4", "x"])
def test_malformed_or_repeated_or_oversized_counter_counts_as_absent(tmp_path, value):
    # Whole-token parsing: `10oops` is not 10, a value wider than nine digits
    # would wrap negative in bash arithmetic and never reach the cap, and a
    # repeated token is ambiguous — each counts as absent (0), never as an
    # escalation or a gate failure.
    body = f"{MARKER}\n🤖 auto review rounds: {value} (cap 10)."
    r, o, _ = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                  comment(100, "i-am-marvin", body, T0)])
    assert o["act"] == "fix" and o["round"] == "1", value
    assert "counting from 0" in r.stdout


def test_counter_is_read_as_decimal(tmp_path):
    # `09` is invalid octal: bash arithmetic on it fails the gate (set -e)
    # unless the value is forced decimal; 9 + 1 = 10 is not past the cap.
    _, o, _ = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                  counter("i-am-marvin", "09", T0, cid=100)])
    assert o["act"] == "fix" and o["round"] == "10"


def test_head_that_is_not_exactly_a_sha_never_trips_no_progress(tmp_path):
    for head in (HEAD + "g", HEAD[:-1], HEAD.upper()):
        _, o, _ = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                      counter("i-am-marvin", 1, T0, cid=100, head=head)])
        assert o["act"] == "fix" and o["round"] == "2", head
        assert o.get("stalled") is None, head


def test_no_progress_escalation_still_fires_on_the_loops_own_counter(tmp_path):
    _, o, _ = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                  counter("i-am-marvin", 1, T0, cid=100, head=HEAD)])
    assert o["act"] == "escalate"
    assert o["stalled"] == "1"


# --- the re-review requests (stale-verdict guard) ---------------------------


def test_outsider_rereview_request_does_not_skip_the_verdict(tmp_path):
    _, o, state = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                      comment(2, "nobody", "@review", T2)],
                           perms={"nobody": "read"})
    assert o["act"] == "fix"
    assert lookups(state) == ["nobody"]


def test_trusted_login_rereview_request_skips_without_a_lookup(tmp_path):
    _, o, state = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                      comment(2, "i-am-marvin", "@review", T2)])
    assert o["act"] == "skip"
    assert lookups(state) == []


def test_write_access_rereview_request_skips_with_one_cached_lookup(tmp_path):
    _, o, state = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                      comment(2, "alice", "@review", T2),
                                      comment(3, "alice", "@review please", T3)],
                           perms={"alice": "write"})
    assert o["act"] == "skip"
    assert lookups(state) == ["alice"]


def test_failed_permission_lookup_is_untrusted(tmp_path):
    _, o, state = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                      comment(2, "ghost", "@review", T2)])
    assert o["act"] == "fix"
    assert lookups(state) and set(lookups(state)) == {"ghost"}


def test_other_bots_rereview_request_is_untrusted_without_a_lookup(tmp_path):
    _, o, state = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                      comment(2, "github-actions[bot]", "@review", T2)])
    assert o["act"] == "fix"
    assert lookups(state) == []


# --- the converged hand-off's double-post check ------------------------------

HANDOFF_STUB = r"""
gh() {
  case "$1 $2" in
    "api repos/o/r/issues/42/comments") cat "$STATE/comments.json" ;;
    "pr comment") shift 6; printf '%s' "$1" >"$STATE/posted" ;;
    *) echo "unexpected gh call: $*" >&2; return 1 ;;
  esac
}
"""


def run_handoff(tmp_path, comments):
    state = fresh_state(tmp_path)
    (state / "comments.json").write_text(json.dumps(comments))
    env = {"STATE": str(state), "REPO": "o/r", "PR": "42", "MENTION": "someone",
           "CONTINUATION": "false", "CLOSED_AT": "", "MARKER": "<!-- auto-converged -->",
           "TRUSTED_LOGINS": "i-am-marvin"}
    r = sh("bash", "-c", HANDOFF_STUB + lift_step(WORKFLOW, "      - name: Converged handoff"),
           check=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return r, (state / "posted").read_text() if (state / "posted").exists() else None


def test_handoff_dedupes_only_on_the_loops_own_marker(tmp_path):
    _, posted = run_handoff(tmp_path, [comment(1, "nobody", "<!-- auto-converged --> nothing to see", T1)])
    assert posted is not None and posted.startswith("<!-- auto-converged -->")
    assert "@someone" in posted
    r, posted = run_handoff(tmp_path, [comment(1, "i-am-marvin", "<!-- auto-converged --> done", T1)])
    assert posted is None
    assert "already posted" in r.stdout


# --- the land job's refund ---------------------------------------------------

REFUND_STUB = r"""
gh() {
  case "$*" in
    "api repos/o/r/issues/42/comments --paginate") cat "$STATE/comments.json" ;;
    "api repos/o/r/issues/comments/"*" --jq .body")
      local id=${2##*/}
      jq -r --argjson i "$id" '.[] | select(.id==$i) | .body' "$STATE/comments.json" ;;
    "api -X PATCH repos/o/r/issues/comments/"*)
      echo "${4##*/}" >>"$STATE/patched"; printf '%s\n' "$6" >"$STATE/patched-body" ;;
    *) echo "unexpected gh call: $*" >&2; return 1 ;;
  esac
}
"""


def run_refund(tmp_path, comments):
    state = fresh_state(tmp_path)
    (state / "comments.json").write_text(json.dumps(comments))
    env = {"STATE": str(state), "REPO": "o/r", "PR": "42", "ROUND": "3", "CAP": "10",
           "HEAD": HEAD, "MARKER": MARKER, "TRUSTED_LOGINS": "i-am-marvin"}
    r = sh("bash", "-c", REFUND_STUB + lift_step(WORKFLOW, "      - name: Refund infra-crashed round"),
           check=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    patched = (state / "patched").read_text().split() if (state / "patched").exists() else []
    body = (state / "patched-body").read_text() if (state / "patched-body").exists() else ""
    return r, patched, body


def test_refund_patches_only_the_loops_own_counter(tmp_path):
    _, patched, body = run_refund(tmp_path, [counter("i-am-marvin", 3, T0, cid=100, head=OLD),
                                             counter("nobody", 999, T2, cid=200, head=HEAD)])
    assert patched == ["100"]
    assert "rounds: 2" in body and OLD in body


def test_refund_ignores_a_counter_that_is_only_an_outsiders(tmp_path):
    r, patched, _ = run_refund(tmp_path, [counter("nobody", 999, T2, cid=200, head=HEAD)])
    assert patched == []
    assert "No counter comment found" in r.stdout


def test_refund_falls_back_to_the_gates_values_on_a_malformed_body(tmp_path):
    body = f"{MARKER}\n🤖 auto review rounds: 3oops (cap 10).\n<!-- auto-review-head:{OLD}g -->"
    _, patched, patched_body = run_refund(tmp_path, [comment(100, "i-am-marvin", body, T0)])
    assert patched == ["100"]
    assert "rounds: 2" in patched_body and HEAD in patched_body, "ROUND=3 and HEAD, the gate's values"


# --- escalation's reset (the shared composite) -------------------------------
#
# The escalation hand-off promises "re-add the label and the loop starts a
# fresh budget". That holds only if the reset PATCHes the SAME counter the
# gate reads: selecting the newest marker regardless of author edited an
# outsider's forgery, left the loop's own counter exhausted, and the next
# verdict escalated again. The stub applies each PATCH to the comment list,
# so the sequence gate → reset → gate runs against the state it produces.

RESET_STUB = r"""
sleep() { :; }
gh() {
  case "$*" in
    "api repos/o/r/issues/42/comments --paginate") cat "$STATE/comments.json" ;;
    "api -X PATCH repos/o/r/issues/comments/"*)
      local id=${4##*/} body=${6#body=}
      echo "$id" >>"$STATE/patched"
      jq --argjson i "$id" --arg b "$body" 'map(if .id == $i then .body = $b else . end)' \
        "$STATE/comments.json" >"$STATE/comments.new" && mv "$STATE/comments.new" "$STATE/comments.json" ;;
    *) echo "unexpected gh call: $*" >&2; return 1 ;;
  esac
}
"""


def run_reset(state: Path, trusted_logins: str):
    out = state.parent / "reset-out"
    out.write_text("")
    env = {"STATE": str(state), "GITHUB_OUTPUT": str(out), "REPO": "o/r", "PR": "42",
           "COUNTERS": "rounds", "REASON": "on escalation", "TRUSTED_LOGINS": trusted_logins}
    r = sh("bash", "-c", RESET_STUB + lift_step(RESET, "    - id: reset"), check=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    patched = (state / "patched").read_text().split() if (state / "patched").exists() else []
    return outputs(out), patched


def test_escalation_reset_targets_the_counter_the_gate_selected(tmp_path):
    at_cap = [verdict("i-am-marvin", "suggestions", T1, cid=1),
              counter("i-am-marvin", 10, T0, cid=100, head=OLD),
              counter("nobody", 999, T2, cid=200, head=HEAD)]
    _, o, state = run_gate(tmp_path, at_cap)
    assert o["act"] == "escalate" and o["cid"] == "100"
    reset, patched = run_reset(state, "i-am-marvin")
    assert reset["ok"] == "1"
    assert patched == ["100"], "the loop's counter, never the outsider's marker"
    # The fresh budget is real: the next verdict runs round 1, on the reset comment.
    comments = json.loads((state / "comments.json").read_text())
    _, o, _ = run_gate(tmp_path, comments)
    assert o["act"] == "fix" and o["round"] == "1" and o["cid"] == "100"


def test_reset_without_trusted_logins_keeps_the_any_author_lookup(tmp_path):
    # The default for the composite's other callers: unchanged behaviour.
    state = fresh_state(tmp_path)
    (state / "comments.json").write_text(json.dumps([counter("i-am-marvin", 10, T0, cid=100),
                                                     counter("nobody", 999, T2, cid=200)]))
    reset, patched = run_reset(state, "")
    assert reset["ok"] == "1" and patched == ["200"]
