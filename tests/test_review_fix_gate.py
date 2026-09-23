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
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_helpers import sh  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "claude-auto-review.yml"
RESET = ROOT / ".github" / "actions" / "reset-auto-counters" / "action.yml"

# The runner's shell options, so a bare non-zero status fails here as it
# would there: GitHub runs a workflow `run:` step (no `shell:` key) with
# `bash -e {0}` and a composite's `shell: bash` step with
# `bash --noprofile --norc -eo pipefail {0}`.
STEP_BASH = ("bash", "--noprofile", "--norc", "-e", "-c")
COMPOSITE_BASH = ("bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c")

HEAD = "a" * 40
OLD = "b" * 40
MARKER = "<!-- auto-review-rounds -->"


def workflow_env(workflow: Path, name: str) -> str:
    """A workflow-level `env:` value, read from the file — the value the
    runner hands every step, so the lifted steps run against the real one."""
    m = re.search(rf"\nenv:\n(?:  .*\n)*?  {name}: (.*)\n", workflow.read_text())
    assert m, name
    return m.group(1).strip()


# The machine account's two logins: the User (the PAT, today) and the GitHub
# App's bot login (Phase 2). Both are in every TRUSTED_LOGINS value until the
# PAT is retired.
MARVIN = "i-am-marvin"
MARVIN_BOT = "meridian-marvin[bot]"
TRUSTED_LOGINS = workflow_env(WORKFLOW, "TRUSTED_LOGINS")
REVIEWER_LOGINS = workflow_env(WORKFLOW, "REVIEWER_LOGINS")
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


def comment(cid, login, body, at, type_=None):
    # A GitHub App's user object is type "Bot" whether or not the login
    # carries a `[bot]` suffix (Copilot's does not).
    if type_ is None:
        type_ = "Bot" if login.endswith("[bot]") else "User"
    return {"id": cid, "user": {"login": login, "type": type_}, "body": body, "created_at": at}


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
        "TRUSTED_LOGINS": TRUSTED_LOGINS, "REVIEWER_LOGINS": REVIEWER_LOGINS,
        "ALLOWED_BOTS": "",
    }
    env.update(env_extra or {})
    r = sh(*STEP_BASH, GH_STUB + lift_step(WORKFLOW, "        id: gate"), check=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return r, outputs(out), state


# --- the verdict ------------------------------------------------------------


def test_forged_clean_verdict_from_an_outsider_is_ignored(tmp_path):
    _, o, _ = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
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


@pytest.mark.parametrize("value", ["10oops", "10.5", "9223372036854775807", "3\\nrounds: 4",
                                   "x\\nrounds: 10", "x"])
def test_malformed_or_repeated_or_oversized_counter_counts_as_absent(tmp_path, value):
    # Whole-token parsing up to the format's own delimiter (whitespace):
    # `10oops` and `10.5` are not 10, a value wider than nine digits would
    # wrap negative in bash arithmetic and never reach the cap, and a
    # repeated token is ambiguous whichever of the two is well-formed — each
    # counts as absent (0), never as an escalation or a gate failure.
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
    for head in (HEAD + "g", HEAD + "-extra", HEAD[:-1], HEAD.upper()):
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


def review_allowed_bots_default() -> str:
    """The `review_allowed_bots` input's default, read from the workflow."""
    text = WORKFLOW.read_text()
    block = text[text.index("      review_allowed_bots:"):]
    block = block[:re.search(r"\n {6}\S", block[1:]).start() + 1]  # up to the next input key
    return re.search(r'default: "([^"]*)"', block).group(1)


def test_reviewer_identitys_request_is_pending_only_where_the_caller_allow_lists_it(tmp_path):
    # claude[bot] is a verdict author, not a requester by right: its `@review`
    # counts through review_allowed_bots like any bot's. The input's DEFAULT
    # names it — the deployed reviewer stubs that allow-list a bot name the
    # reviewer bot, and the loop counted its requests before the author
    # filters — so a caller passing nothing keeps that; one whose reviewer
    # admits no bot passes an explicit empty string and the gate no longer
    # waits for a review that never runs.
    comments = [verdict("i-am-marvin", "suggestions", T1, cid=1), comment(2, "claude[bot]", "@review", T2)]
    assert review_allowed_bots_default() == "claude[bot]"
    _, o, state = run_gate(tmp_path, comments, env_extra={"ALLOWED_BOTS": review_allowed_bots_default()})
    assert o["act"] == "skip" and lookups(state) == []
    _, o, state = run_gate(tmp_path, comments, env_extra={"ALLOWED_BOTS": ""})
    assert o["act"] == "fix" and lookups(state) == []


@pytest.mark.parametrize("allowed", ["ci-helper[bot]", "ci-helper", "CI-Helper[bot]", "*", "other, ci-helper"])
def test_a_bot_in_review_allowed_bots_has_a_pending_request(tmp_path, allowed):
    # A caller's reviewer honours `@review` from the bots in its allowed_bots;
    # the same list here makes their requests pending, with no lookup.
    _, o, state = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                      comment(2, "ci-helper[bot]", "@review", T2)],
                           env_extra={"ALLOWED_BOTS": allowed})
    assert o["act"] == "skip", allowed
    assert lookups(state) == []


def test_a_suffixless_app_actor_is_a_bot_by_payload_type(tmp_path):
    # Copilot: type Bot, no `[bot]` suffix — allow-listed it is pending,
    # otherwise refused without a permission lookup (the endpoint 404s for
    # Apps); never mistaken for a user.
    comments = [verdict("i-am-marvin", "suggestions", T1, cid=1), comment(2, "Copilot", "@review", T2, type_="Bot")]
    _, o, state = run_gate(tmp_path, comments, env_extra={"ALLOWED_BOTS": "Copilot"})
    assert o["act"] == "skip" and lookups(state) == []
    _, o, state = run_gate(tmp_path, comments)
    assert o["act"] == "fix" and lookups(state) == []


def test_other_bots_rereview_request_is_untrusted_without_a_lookup(tmp_path):
    for allowed in ("", "*", "github-actions[bot]"):
        _, o, state = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                          comment(2, "github-actions[bot]", "@review", T2)],
                               env_extra={"ALLOWED_BOTS": allowed})
        assert o["act"] == "fix", allowed
        assert lookups(state) == []
    _, o, state = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                      comment(2, "ci-helper[bot]", "@review", T2)])
    assert o["act"] == "fix" and lookups(state) == [], "not allow-listed: refused, never looked up"


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
           "TRUSTED_LOGINS": TRUSTED_LOGINS}
    r = sh(*STEP_BASH, HANDOFF_STUB + lift_step(WORKFLOW, "      - name: Converged handoff"),
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
      [ -f "$STATE/body-fail" ] && { echo '{"message":"Server Error"}'; return 1; }
      local id=${2##*/}
      jq -r --argjson i "$id" '.[] | select(.id==$i) | .body' "$STATE/comments.json" ;;
    "api -X PATCH repos/o/r/issues/comments/"*)
      echo "${4##*/}" >>"$STATE/patched"; printf '%s\n' "$6" >"$STATE/patched-body" ;;
    *) echo "unexpected gh call: $*" >&2; return 1 ;;
  esac
}
"""


def run_refund(tmp_path, comments, *, body_fail=False):
    state = fresh_state(tmp_path)
    (state / "comments.json").write_text(json.dumps(comments))
    if body_fail:
        (state / "body-fail").write_text("")
    env = {"STATE": str(state), "REPO": "o/r", "PR": "42", "ROUND": "3", "CAP": "10",
           "MARKER": MARKER, "TRUSTED_LOGINS": TRUSTED_LOGINS}
    r = sh(*STEP_BASH, REFUND_STUB + lift_step(WORKFLOW, "      - name: Refund infra-crashed round"),
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


def test_refund_skips_when_the_counter_body_cannot_be_read(tmp_path):
    # A failed GET is not an absent count: zeroing the counter on it would
    # erase the budget and the no-progress baseline. No PATCH, a warning,
    # one round of slack.
    r, patched, _ = run_refund(tmp_path, [counter("i-am-marvin", 3, T0, cid=100, head=OLD)], body_fail=True)
    assert patched == []
    assert "::warning::could not read counter comment 100" in r.stdout


def test_refund_ignores_a_counter_that_is_only_an_outsiders(tmp_path):
    r, patched, _ = run_refund(tmp_path, [counter("nobody", 999, T2, cid=200, head=HEAD)])
    assert patched == []
    assert "No counter comment found" in r.stdout


def test_refund_reads_an_absent_or_unparsable_count_as_zero_with_no_head(tmp_path):
    # A reset body (escalation or re-engagement) carries no count and no
    # head on purpose; a late refund that wrote this run's round (3) and tip
    # back over it would undo the fresh budget and re-arm the no-progress
    # check. From 0 the refund stays at 0 and records no head, so the next
    # gate runs round 1 with a fresh baseline. A malformed body reads the same.
    reset_body = f"{MARKER}\n🤖 auto review rounds reset (on escalation) — the next round starts at 1 with the full cap."
    _, patched, patched_body = run_refund(tmp_path, [comment(100, "i-am-marvin", reset_body, T0)])
    assert patched == ["100"]
    assert "rounds: 0" in patched_body and "auto-review-head:" not in patched_body
    _, o, _ = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                  comment(100, "i-am-marvin", patched_body, T0)])
    assert o["act"] == "fix" and o["round"] == "1" and o.get("stalled") is None
    malformed = f"{MARKER}\n🤖 auto review rounds: 3.5 (cap 10).\n<!-- auto-review-head:{OLD}-x -->"
    _, patched, patched_body = run_refund(tmp_path, [comment(100, "i-am-marvin", malformed, T0)])
    assert patched == ["100"] and "rounds: 0" in patched_body and "auto-review-head:" not in patched_body


# --- the refund's trust and the stall check it must not disarm (4628734) -----
#
# The finding's loop: a steered fix agent sets `handback: true`, commits
# nothing and kills its own action step. The refund read the step's failure
# and an empty execution file as an infra crash and took round 1 back to 0
# (marker kept); the bare `@review` posted anyway; the next verdict found
# prev=0 and skipped the stall check, so round 1 ran again — without bound.
# Three rules close it, each checked here or in the composer / land tests:
# the refund fires only on a step the runner never entered, a bundle-less
# hand-back needs a successful agent step, and the stall check keys on the
# recorded tip whatever the count reads.


def test_no_progress_escalation_fires_at_zero_when_the_recorded_tip_is_unchanged(tmp_path):
    # rounds: 0 with a head marker is exactly the refunded-round-1 body; the
    # unchanged tip escalates, a moved tip runs round 1.
    _, o, _ = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                  counter("i-am-marvin", 0, T0, cid=100, head=HEAD)])
    assert o["act"] == "escalate" and o["stalled"] == "1"
    _, o, _ = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                  counter("i-am-marvin", 0, T0, cid=100, head=OLD)])
    assert o["act"] == "fix" and o["round"] == "1" and o.get("stalled") is None


def test_a_refunded_first_round_still_escalates_on_the_unchanged_tip(tmp_path):
    # gate (round 1 recorded on HEAD) → refund → gate on the same tip. The
    # refund writes rounds: 0 and keeps the tip; the next verdict on that tip
    # escalates for no progress instead of re-running round 1 — and a refund
    # never takes the count below 0.
    _, patched, refunded = run_refund(tmp_path, [counter("i-am-marvin", 1, T0, cid=100, head=HEAD)])
    assert patched == ["100"] and "rounds: 0" in refunded and HEAD in refunded
    _, o, _ = run_gate(tmp_path, [verdict("i-am-marvin", "suggestions", T1, cid=1),
                                  comment(100, "i-am-marvin", refunded, T0)])
    assert o["act"] == "escalate" and o["stalled"] == "1"
    _, patched, again = run_refund(tmp_path, [comment(100, "i-am-marvin", refunded, T0)])
    assert patched == ["100"] and "rounds: 0" in again and HEAD in again


def step_if(workflow: Path, anchor: str) -> str:
    """The `if: >-` expression of the step at `anchor`, whitespace-folded."""
    text = workflow.read_text()
    block = text[text.index(anchor):]
    block = block[:block.index("        run: |")]
    return " ".join(block[block.index("if: >-") + len("if: >-"):block.index("env:")].split())


def ghx(expr: str, ctx: dict) -> bool:
    """Evaluate a GitHub Actions `if:` expression of the shape the refund
    steps use — `always()`, `<context path> == 'literal'` / `!= 'literal'`,
    `&&`, `||`, parentheses — against `ctx`, a map of context paths to their
    string values. A path missing from `ctx` is an unset output or an
    undelivered job output and compares as the empty string, as it does on
    the runner. Written for these tests only; it refuses anything else."""
    tokens = re.findall(r"\(|\)|&&|\|\||==|!=|'[^']*'|always\(\)|[A-Za-z_][\w.-]*", expr)
    assert "".join(tokens).replace(" ", "") == expr.replace(" ", ""), f"unsupported syntax in {expr!r}"
    pos = [0]

    def peek():
        return tokens[pos[0]] if pos[0] < len(tokens) else None

    def take():
        pos[0] += 1
        return tokens[pos[0] - 1]

    def atom():
        t = take()
        if t == "(":
            v = expr_or()
            assert take() == ")"
            return v
        if t == "always()":
            return True
        assert re.match(r"[A-Za-z_]", t) and peek() in ("==", "!="), t
        op, lit = take(), take()
        assert lit.startswith("'"), lit
        left = ctx.get(t, "")
        return (left == lit[1:-1]) if op == "==" else (left != lit[1:-1])

    def expr_and():
        v = atom()
        while peek() == "&&":
            take()
            v = atom() and v
        return v

    def expr_or():
        v = expr_and()
        while peek() == "||":
            take()
            v = expr_and() or v
        return v

    v = expr_or()
    assert peek() is None, tokens[pos[0]:]
    return v


# One row per shape the land job can meet; `fix_result` is `needs.fix.result`,
# `skipped` the fix job's `agent_skipped` output (None: the job delivered no
# outputs — a PENDING job cancelled before it started, or a runner that
# died), `pushed` the Land step's output. Shared with test_ci_fix_gate.py:
# the two refunds must read identically.
REFUND_CASES = [
    # (fix_result, skipped, pushed, refunded, why)
    ("failure", "true", "", True, "sync or provisioning failed; the agent step was skipped"),
    ("cancelled", "true", "", True, "cancelled during checkout/sync/provisioning; the agent step was skipped"),
    ("failure", "false", "", False, "the agent step was entered and failed (or was killed by the agent)"),
    ("cancelled", "false", "", False, "cancelled after the agent step started: the agent ran"),
    ("cancelled", None, "", False, "a pending job cancelled before it started delivers no outputs: unknown keeps its round"),
    ("failure", None, "", False, "no outputs delivered (runner died mid-agent): unknown keeps its round"),
    ("success", "false", "1", False, "a normal round that landed"),
    ("success", "false", "", False, "a round that ran and landed nothing (no-change, or a lost bundle)"),
    ("failure", "true", "1", False, "provisioning failed over a stale branch but the base merge landed"),
]


def refund_ctx(fix_result, skipped, pushed, act="fix"):
    ctx = {"needs.gate.outputs.act": act, "needs.fix.result": fix_result, "steps.land.outputs.pushed": pushed}
    if skipped is not None:
        ctx["needs.fix.outputs.agent_skipped"] = skipped
    return ctx


def engine_selected_skipped(engine, skipped):
    """The land job's `AGENT_SKIPPED` env — `engine == 'codex' &&
    needs.fix-codex.outputs.agent_skipped || needs.fix.outputs.agent_skipped`
    — as GitHub evaluates `a && b || c` (b when a holds and b is non-empty,
    else c): only the engine's job ran and delivered `skipped`; the other
    job was skipped at the job level and delivers no outputs (one job per
    engine, findings 4628446 and 4629153)."""
    codex_out = (skipped or "") if engine == "codex" else ""
    claude_out = (skipped or "") if engine != "codex" else ""
    return codex_out if engine == "codex" and codex_out else claude_out


def refund_ctx_split(fix_result, skipped, pushed, engine, act="fix"):
    ctx = {"needs.gate.outputs.act": act, "steps.land.outputs.pushed": pushed,
           "env.AGENT_RESULT": fix_result, "env.AGENT_SKIPPED": engine_selected_skipped(engine, skipped)}
    return ctx


def test_the_refund_and_the_hand_back_key_on_evidence_settled_before_the_agent_ran():
    """The refund's gating inputs (the `if:`, not only the comment selection
    the tests above cover): the fix job's `agent_skipped` output — both
    engines' agent steps `skipped`, a step outcome settled before any agent
    code ran — plus nothing pushed. Never the agent step's own outcome, and
    never the job's RESULT (review round 1: a job cancelled after the agent
    step started ran the agent, and a pending job cancelled before it
    started delivers no outputs, so neither is evidence — unknown keeps its
    round). No execution-file signal exists in the workflow at all. The Land
    step admits a bundle-less hand-back only on the agent step's success,
    the direction the agent cannot push."""
    text = WORKFLOW.read_text()
    assert "id: launched" not in text and "agent_started" not in text and 'echo "value=true"' not in text
    # One job per engine: each job's `agent_skipped` reads its own agent
    # step, and the land job selects the engine's.
    claude_job = text[text.index("\n  fix:\n"):text.index("\n  fix-codex:\n")]
    codex_job = text[text.index("\n  fix-codex:\n"):text.index("\n  land:\n")]
    land_job = text[text.index("\n  land:\n"):]
    assert "      agent_skipped: ${{ steps.claude.outcome == 'skipped' && 'true' || 'false' }}\n" in claude_job
    assert "      agent_skipped: ${{ steps.codexfix.outcome == 'skipped' && 'true' || 'false' }}\n" in codex_job
    assert "      agent_outcome: ${{ steps.claude.outcome }}\n" in claude_job
    assert ("      AGENT_SKIPPED: ${{ needs.gate.outputs.engine == 'codex' && needs.fix-codex.outputs.agent_skipped "
            "|| needs.fix.outputs.agent_skipped }}\n") in land_job
    outputs = claude_job[claude_job.index("    outputs:\n"):claude_job.index("    steps:\n")]
    assert "outputs.value" not in outputs and "-s " not in outputs
    condition = step_if(WORKFLOW, "      - name: Refund infra-crashed round")
    assert condition == ("always() && needs.gate.outputs.act == 'fix' && "
                         "env.AGENT_SKIPPED == 'true' && "
                         "steps.land.outputs.pushed != '1'")
    assert "needs.fix.result" not in condition and "agent_outcome" not in condition and "AGENT_RESULT" not in condition
    for engine in ("claude", "codex"):
        for fix_result, skipped, pushed, refunded, why in REFUND_CASES:
            assert ghx(condition, refund_ctx_split(fix_result, skipped, pushed, engine)) is refunded, (engine, why)
        assert ghx(condition, refund_ctx_split("failure", "true", "", engine, act="escalate")) is False
    land = text[text.index("      - name: Land\n"):text.index("      # Infra crashes must not burn review rounds")]
    assert "allow-no-change-handback: ${{ env.AGENT_OUTCOME == 'success' && 'true' || 'false' }}" in land


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
    "api repos/o/r/collaborators/"*"/permission --jq .permission")
      local login=${2#repos/o/r/collaborators/}; login=${login%/permission}
      echo "$login" >>"$STATE/lookups"
      local p; p=$(awk -v l="$login" '$1==l {print $2}' "$STATE/perms")
      [ -n "$p" ] || { echo '{"message":"Not Found"}'; return 1; }
      echo "$p" ;;
    "api -X PATCH repos/o/r/issues/comments/"*)
      local id=${4##*/} body=${6#body=}
      echo "$id" >>"$STATE/patched"
      jq --argjson i "$id" --arg b "$body" 'map(if .id == $i then .body = $b else . end)' \
        "$STATE/comments.json" >"$STATE/comments.new" && mv "$STATE/comments.new" "$STATE/comments.json" ;;
    *) echo "unexpected gh call: $*" >&2; return 1 ;;
  esac
}
"""


def run_reset(state: Path, *, comment_id="", trusted_logins="", counters="rounds", perms=None):
    out = state.parent / "reset-out"
    out.write_text("")
    (state / "perms").write_text("".join(f"{k} {v}\n" for k, v in (perms or {}).items()))
    env = {"STATE": str(state), "GITHUB_OUTPUT": str(out), "REPO": "o/r", "PR": "42",
           "COUNTERS": counters, "REASON": "on escalation", "COMMENT_ID": comment_id,
           "TRUSTED_LOGINS": trusted_logins}
    r = sh(*COMPOSITE_BASH, RESET_STUB + lift_step(RESET, "    - id: reset"), check=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    patched = (state / "patched").read_text().split() if (state / "patched").exists() else []
    return outputs(out), patched


def reset_trusted_logins_default() -> str:
    """The reset composite's `trusted-logins` default, read from the action."""
    text = RESET.read_text()
    block = text[text.index("  trusted-logins:"):]
    block = block[:re.search(r"\n {2}\S", block[1:]).start() + 1]  # up to the next input key
    return re.search(r"default: (.*)", block).group(1).strip()


def test_reset_default_trusts_the_machine_account_under_both_logins(tmp_path):
    # A caller that passes no trusted-logins still resets the loops' own
    # counters — under Phase 2 the App's, which the attempts fallback would
    # otherwise refuse as an App's marker. An explicit empty string is the
    # opt-out: nothing is the loop's own, so nothing is reset.
    assert reset_trusted_logins_default() == f"{MARVIN},{MARVIN_BOT}"
    comments = [counter(MARVIN_BOT, 10, T0, cid=100),
                comment(101, MARVIN_BOT, "<!-- auto-fix-attempts -->\nattempts: 3 (cap 3).", T0)]
    state = fresh_state(tmp_path)
    (state / "comments.json").write_text(json.dumps(comments))
    reset, patched = run_reset(state, trusted_logins=reset_trusted_logins_default(), counters="rounds attempts")
    assert reset["ok"] == "1" and sorted(patched) == ["100", "101"] and lookups(state) == []
    state = fresh_state(tmp_path)
    (state / "comments.json").write_text(json.dumps(comments))
    reset, patched = run_reset(state, trusted_logins="", counters="rounds attempts")
    assert reset["ok"] == "1" and patched == [] and lookups(state) == []


def test_escalation_reset_targets_the_counter_the_gate_selected(tmp_path):
    at_cap = [verdict("i-am-marvin", "suggestions", T1, cid=1),
              counter("i-am-marvin", 10, T0, cid=100, head=OLD),
              counter("nobody", 999, T2, cid=200, head=HEAD)]
    _, o, state = run_gate(tmp_path, at_cap)
    assert o["act"] == "escalate" and o["cid"] == "100"
    # As the workflow calls it: the gate's cid as comment-id, TRUSTED_LOGINS
    # as the fallback lookup's filter.
    reset, patched = run_reset(state, comment_id=o["cid"], trusted_logins="i-am-marvin")
    assert reset["ok"] == "1"
    assert patched == ["100"], "the loop's counter, never the outsider's marker"
    # The fresh budget is real: the next verdict runs round 1, on the reset comment.
    comments = json.loads((state / "comments.json").read_text())
    _, o, _ = run_gate(tmp_path, comments)
    assert o["act"] == "fix" and o["round"] == "1" and o["cid"] == "100"


def test_reset_lookup_follows_the_review_gates_rule_for_rounds(tmp_path):
    # No cid (the re-engagement reset has no gate): the rounds counter is the
    # loop's own newest marker — never a newer forgery, and never a
    # write-access human's either, since the review gate reads neither.
    comments = [counter("i-am-marvin", 10, T0, cid=100), counter("nobody", 999, T2, cid=200),
                counter("alice", 5, T3, cid=300)]
    state = fresh_state(tmp_path)
    (state / "comments.json").write_text(json.dumps(comments))
    reset, patched = run_reset(state, trusted_logins="i-am-marvin", perms={"alice": "write"})
    assert reset["ok"] == "1" and patched == ["100"]
    assert lookups(state) == [], "the review gate never looks up a rounds author"
    # Only a maintainer's rounds marker: nothing the review gate would count.
    state = fresh_state(tmp_path)
    (state / "comments.json").write_text(json.dumps([counter("alice", 5, T3, cid=300)]))
    reset, patched = run_reset(state, trusted_logins="i-am-marvin", perms={"alice": "write"})
    assert reset["ok"] == "1" and patched == [] and lookups(state) == []
    # No trusted logins at all: no comment is the loop's own.
    state = fresh_state(tmp_path)
    (state / "comments.json").write_text(json.dumps(comments))
    reset, patched = run_reset(state)
    assert reset["ok"] == "1" and patched == []


def test_reset_ignores_a_comment_id_when_several_counters_are_requested(tmp_path):
    # One comment is one counter: with both counters requested the id names
    # neither, so each is looked up (the rounds marker resets, no attempts
    # marker exists) and the caller is told.
    state = fresh_state(tmp_path)
    (state / "comments.json").write_text(json.dumps([counter("i-am-marvin", 10, T0, cid=100)]))
    out = state.parent / "reset-out"
    env = {"STATE": str(state), "GITHUB_OUTPUT": str(out), "REPO": "o/r", "PR": "42",
           "COUNTERS": "rounds attempts", "REASON": "on re-engagement", "COMMENT_ID": "100",
           "TRUSTED_LOGINS": "i-am-marvin"}
    out.write_text("")
    r = sh(*COMPOSITE_BASH, RESET_STUB + lift_step(RESET, "    - id: reset"), check=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "looking each up instead" in r.stdout
    assert (state / "patched").read_text().split() == ["100"]
    assert outputs(out)["ok"] == "1"


# --- one trusted-logins value per workflow -----------------------------------


def test_workflow_declares_trusted_logins_once_and_passes_it_to_every_composite():
    # The env names the machine account under both logins (Phase 2 adds the
    # GitHub App's; retiring the PAT removes the User's); every composite
    # that decides trust reads it from there (the labeler check and the
    # escalation reset), and no step — the gate's @-mention derivation
    # included — names a login itself. The reviewer bot is a verdict author
    # next to both.
    text = WORKFLOW.read_text()
    assert text.count("\nenv:\n") == 1
    assert TRUSTED_LOGINS == f"{MARVIN},{MARVIN_BOT}"
    assert REVIEWER_LOGINS == f"{MARVIN},{MARVIN_BOT},claude[bot]"
    assert text.count("trusted-logins: ${{ env.TRUSTED_LOGINS }}") == 2
    for anchor in ("        id: resolve", "        id: gate", "      - name: Converged handoff",
                   "      - name: Refund infra-crashed round"):
        assert "i-am-marvin" not in lift_step(WORKFLOW, anchor), anchor
    assert "trusted_author" in lift_step(WORKFLOW, "        id: gate")
    # The fix agent's bot allow-lists carry the same value (the codex verdict
    # is posted by the machine account, a bot actor under Phase 2).
    assert "allowed_bots: ${{ format('claude,{0}', env.TRUSTED_LOGINS) }}" in text
    assert "allow-bot-users: ${{ format('claude,{0}', env.TRUSTED_LOGINS) }}" in text


@pytest.mark.parametrize("login", [MARVIN, MARVIN_BOT])
def test_machine_account_is_verdict_author_counter_owner_and_requester_under_either_login(tmp_path, login):
    # Phase 2: the land job posts the codex verdict, the round counter and the
    # hand-back as the App's bot login. No lookup anywhere — the collaborators
    # endpoint answers `none` for an App, so trust is by name.
    _, o, state = run_gate(tmp_path, [verdict(login, "clean", T1)])
    assert o["act"] == "converged" and lookups(state) == []
    _, o, state = run_gate(tmp_path, [verdict(login, "suggestions", T1, cid=1),
                                      counter(login, 2, T0, cid=100, head=OLD)])
    assert o["act"] == "fix" and o["round"] == "3" and o["cid"] == "100" and lookups(state) == []
    _, o, state = run_gate(tmp_path, [verdict(MARVIN, "suggestions", T1, cid=1), comment(2, login, "@review", T2)])
    assert o["act"] == "skip" and lookups(state) == []


@pytest.mark.parametrize("bot", ["github-actions[bot]", "foo[bot]"])
def test_other_bots_are_neither_verdict_authors_nor_counters_nor_requesters(tmp_path, bot):
    _, o, _ = run_gate(tmp_path, [verdict(MARVIN, "suggestions", T1, cid=1), verdict(bot, "clean", T2, cid=2)])
    assert o["verdict"] == "suggestions" and o["act"] == "fix"
    _, o, _ = run_gate(tmp_path, [verdict(MARVIN, "suggestions", T1, cid=1),
                                  counter(bot, 999, T2, cid=200, head=HEAD)])
    assert o["act"] == "fix" and o["round"] == "1" and o["cid"] == ""
    _, o, state = run_gate(tmp_path, [verdict(MARVIN, "suggestions", T1, cid=1), comment(2, bot, "@review", T2)])
    assert o["act"] == "fix" and lookups(state) == []


# --- the resolve step's author check ------------------------------------------


RESOLVE_STUB = r"""
sleep() { :; }
gh() {
  case "$*" in
    "pr view 42 --repo o/r --json "*) cat "$STATE/pr.json" ;;
    *) echo "unexpected gh call: $*" >&2; return 1 ;;
  esac
}
"""


def run_resolve(tmp_path, author):
    state = fresh_state(tmp_path)
    (state / "pr.json").write_text(json.dumps({
        "state": "OPEN", "closedAt": None, "headRefName": "claude/issue-9-x", "headRefOid": HEAD,
        "isCrossRepository": False, "labels": [{"name": "auto"}]}))
    out = tmp_path / "out"
    out.write_text("")
    env = {"GITHUB_OUTPUT": str(out), "STATE": str(state), "REPO": "o/r", "PR": "42", "AUTHOR": author,
           "REVIEWER": "claude[bot]", "AUTO_LABEL": "auto",
           "TRUSTED_LOGINS": TRUSTED_LOGINS}
    r = sh(*STEP_BASH, RESOLVE_STUB + lift_step(WORKFLOW, "        id: resolve"), check=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return r, outputs(out)


@pytest.mark.parametrize("author", ["claude[bot]", MARVIN, MARVIN_BOT])
def test_resolve_proceeds_on_the_reviewers_and_the_machine_accounts_verdict_comments(tmp_path, author):
    _, o = run_resolve(tmp_path, author)
    assert o["act"] == "verify", author


@pytest.mark.parametrize("author", ["github-actions[bot]", "foo[bot]", "nobody"])
def test_resolve_skips_a_marker_comment_from_anyone_else(tmp_path, author):
    r, o = run_resolve(tmp_path, author)
    assert o["act"] == "skip" and "not the automated reviewer" in r.stdout, author
