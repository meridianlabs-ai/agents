"""Tests for claude-review.yml's `Check trigger` step on comment-triggered
reviews (Claude Security finding 4085111).

Every `@review` comment used to be admitted on a same-repo head whoever
posted it; only fork heads and External issues asked for write access. The
step is lifted out of the workflow (as test_review_fix_gate.py lifts the
review loop's steps) and run against a stub `gh`: an outsider is refused on
every head, a write-access commenter is admitted (sandboxed on a fork head),
the loops' hand-back identity (TRUSTED_LOGINS) is admitted without a lookup,
and a bot only when the caller's allowed_bots names it, same-repo heads only.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_helpers import sh  # noqa: E402
from test_review_fix_gate import STEP_BASH, fresh_state, lift_step, lookups, outputs, workflow_env  # noqa: E402
from test_review_fix_gate import run_gate, verdict, comment, review_allowed_bots_default, T1, T2  # noqa: E402
from test_review_fix_gate import MARVIN, MARVIN_BOT  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "claude-review.yml"
TRUSTED_LOGINS = workflow_env(WORKFLOW, "TRUSTED_LOGINS")
HEAD = "c" * 40
UPSTREAM = "https://github.com/up/stream/pull/12"

GH_STUB = r"""
sleep() { :; }
gh() {
  case "$*" in
    "api repos/o/r/pulls/7") cat "$STATE/pr.json" ;;
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


def run_trig(tmp_path, *, actor, perms=None, allowed_bots="", head_repo="o/r",
             is_pr=True, external=False, ibody="", actor_type=None):
    # The payload's account type: "Bot" for a GitHub App (with or without a
    # `[bot]` suffix), "User" otherwise.
    if actor_type is None:
        actor_type = "Bot" if actor.endswith("[bot]") else "User"
    state = fresh_state(tmp_path)
    (state / "perms").write_text("".join(f"{k} {v}\n" for k, v in (perms or {}).items()))
    (state / "pr.json").write_text(json.dumps({
        "head": {"repo": {"full_name": head_repo}, "sha": HEAD, "ref": "feature"},
        "state": "open", "merged": False}))
    out = tmp_path / "out"
    out.write_text("")
    env = {
        "GITHUB_OUTPUT": str(out), "STATE": str(state), "EVENT": "issue_comment",
        "ACTOR": actor, "GH_REPO": "o/r", "COMMENT": "@review", "IS_PR": "true" if is_pr else "false",
        "PR_NUM": "7", "EXTERNAL": "true" if external else "false", "IBODY": ibody,
        "HEAD_REPO": "", "HEAD_REF": "", "TRUSTED_LOGINS": TRUSTED_LOGINS, "ALLOWED_BOTS": allowed_bots,
        "ACTOR_TYPE": actor_type,
    }
    r = sh(*STEP_BASH, GH_STUB + lift_step(WORKFLOW, "        id: trig"), check=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return r, outputs(out), state


def test_outsider_is_refused_on_a_same_repo_head(tmp_path):
    r, o, state = run_trig(tmp_path, actor="nobody", perms={"nobody": "read"})
    assert o["ok"] == "false" and o["ack"] == "false"
    assert lookups(state) == ["nobody"]
    assert "::warning::" in r.stdout and "refusing" in r.stdout


def test_write_access_commenter_is_admitted_on_a_same_repo_head(tmp_path):
    _, o, _ = run_trig(tmp_path, actor="alice", perms={"alice": "write"})
    assert o["ok"] == "true" and o["ack"] == "true" and o["fork_head"] == "false"
    assert o["head_sha"] == HEAD and o["head_ref"] == "feature"


@pytest.mark.parametrize("perm", ["admin", "maintain"])
def test_admin_and_maintain_count_as_write(tmp_path, perm):
    _, o, _ = run_trig(tmp_path, actor="alice", perms={"alice": perm})
    assert o["ok"] == "true"


def test_trusted_login_is_admitted_without_a_lookup(tmp_path):
    # No `perms` entry: a lookup would fail, so admission proves none ran.
    _, o, state = run_trig(tmp_path, actor="i-am-marvin")
    assert o["ok"] == "true" and o["ack"] == "true"
    assert lookups(state) == []


def test_failed_permission_lookup_refuses(tmp_path):
    _, o, state = run_trig(tmp_path, actor="ghost")
    assert o["ok"] == "false"
    assert set(lookups(state)) == {"ghost"}


@pytest.mark.parametrize("allowed", ["claude[bot]", "claude", "Claude[bot]", "*", "other, claude[bot]"])
def test_allow_listed_bot_is_admitted_on_a_same_repo_head_without_ack(tmp_path, allowed):
    _, o, state = run_trig(tmp_path, actor="claude[bot]", allowed_bots=allowed)
    assert o["ok"] == "true" and o["ack"] == "false", allowed
    assert lookups(state) == []


@pytest.mark.parametrize("allowed", ["", "other[bot]", "github-actions"])
def test_bot_not_in_allowed_bots_is_refused(tmp_path, allowed):
    _, o, state = run_trig(tmp_path, actor="claude[bot]", allowed_bots=allowed)
    assert o["ok"] == "false", allowed
    assert lookups(state) == []
    _, o, state = run_trig(tmp_path, actor="foo[bot]", allowed_bots=allowed or "claude[bot]")
    assert o["ok"] == "false" and lookups(state) == []


def test_machine_account_bot_login_is_admitted_like_the_user_without_a_lookup(tmp_path):
    # Phase 2: the land jobs post the `@review` hand-back as the App's bot
    # login. TRUSTED_LOGINS names it, so it is decided before the [bot] case
    # (no allow-list needed, no lookup — which would answer `none` for an
    # App) and keeps the ack; a fork head takes the sandboxed path, and an
    # External proxy's @review is honoured, exactly as for the User.
    for head_repo, fork in (("o/r", "false"), ("someone/r", "true")):
        _, o, state = run_trig(tmp_path, actor=MARVIN_BOT, head_repo=head_repo)
        assert o["ok"] == "true" and o["ack"] == "true" and o["fork_head"] == fork, head_repo
        assert lookups(state) == []
    _, o, state = run_trig(tmp_path, actor=MARVIN_BOT, is_pr=False, external=True, ibody=f"Upstream PR: {UPSTREAM}")
    assert o["ok"] == "true" and o["mode"] == "external" and lookups(state) == []


def test_workflow_trusts_both_machine_account_logins_and_appends_them_to_the_bot_allow_lists():
    # One env value names the User (the PAT) and the App's bot login; the
    # review step's allowed_bots is the caller's list plus that value (the
    # bare "*" kept as is — claude-code-action recognises only the exact
    # string), and the codex step's allow-bot-users carries it too, so the
    # hand-back posted under Phase 2's identity is not refused by either
    # action's own actor guard after trig admitted it.
    assert TRUSTED_LOGINS == f"{MARVIN},{MARVIN_BOT}"
    text = WORKFLOW.read_text()
    assert text.count("\nenv:\n") == 1
    assert ("allowed_bots: ${{ inputs.allowed_bots == '*' && '*' || (inputs.allowed_bots != '' "
            "&& format('{0},{1}', inputs.allowed_bots, env.TRUSTED_LOGINS) || env.TRUSTED_LOGINS) }}") in text
    assert "allow-bot-users: ${{ format('claude,{0}', env.TRUSTED_LOGINS) }}" in text
    assert "i-am-marvin" not in lift_step(WORKFLOW, "        id: trig")
    assert "i-am-marvin" not in lift_step(WORKFLOW, "      - name: Verify the review was posted")


@pytest.mark.parametrize("allowed", ["*", "github-actions", "github-actions[bot]"])
def test_github_actions_bot_is_never_admitted(tmp_path, allowed):
    # Any repository's workflow run posts as it, so it identifies nobody —
    # no allow-list entry and not even the wildcard admits it.
    _, o, state = run_trig(tmp_path, actor="github-actions[bot]", allowed_bots=allowed)
    assert o["ok"] == "false", allowed
    assert lookups(state) == []


@pytest.mark.parametrize("allowed", ["Copilot", "copilot", "*"])
def test_a_suffixless_app_actor_is_admitted_by_the_allow_list_not_a_lookup(tmp_path, allowed):
    # Copilot posts as `Copilot`, type Bot, no `[bot]` suffix: the allow-list
    # decides, as claude-code-action's actor check does; no collaborator
    # lookup (which would 404 and refuse).
    _, o, state = run_trig(tmp_path, actor="Copilot", actor_type="Bot", allowed_bots=allowed)
    assert o["ok"] == "true" and o["ack"] == "false", allowed
    assert lookups(state) == []
    _, o, state = run_trig(tmp_path, actor="Copilot", actor_type="Bot")
    assert o["ok"] == "false" and lookups(state) == []


def test_a_user_whose_login_ends_in_bot_is_still_a_user(tmp_path):
    # Only the payload type or the literal `[bot]` suffix makes a bot; a
    # human login like `robot` is looked up like any other.
    _, o, state = run_trig(tmp_path, actor="robot", perms={"robot": "write"})
    assert o["ok"] == "true" and lookups(state) == ["robot"]


@pytest.mark.parametrize("bot", ["claude[bot]", "ci-helper[bot]"])
def test_the_gate_treats_a_bots_request_as_pending_exactly_when_the_reviewer_admits_it(tmp_path, bot):
    # Both steps on one fixture, both ways. Allow-listed on both sides (the
    # reviewer stub's allowed_bots and the loop's review_allowed_bots): the
    # reviewer admits the bot's `@review` on a same-repo head, so a review-fix
    # gate queued behind it sees a pending review and skips the verdict that
    # request supersedes (on an unchanged tip it would otherwise escalate for
    # no progress). On neither list: the reviewer refuses the request, so
    # the gate must NOT wait for a review that never runs — for the
    # reviewer's own identity too, once the caller says so explicitly.
    comments = [verdict("i-am-marvin", "suggestions", T1, cid=1), comment(2, bot, "@review", T2)]
    _, o, _ = run_trig(tmp_path, actor=bot, allowed_bots=bot)
    assert o["ok"] == "true"
    _, g, state = run_gate(tmp_path, comments, env_extra={"ALLOWED_BOTS": bot})
    assert g["act"] == "skip" and lookups(state) == []
    _, o, state = run_trig(tmp_path, actor=bot)
    assert o["ok"] == "false" and lookups(state) == []
    _, g, state = run_gate(tmp_path, comments, env_extra={"ALLOWED_BOTS": ""})
    assert g["act"] == "fix" and lookups(state) == []


def test_a_deployed_caller_that_allow_lists_the_reviewer_bot_needs_no_loop_stub_change(tmp_path):
    # The inspect_ai fork's reviewer stub sets `allowed_bots: "claude[bot]"`
    # and its loop stub passes no review_allowed_bots (the input is new):
    # with the loop's DEFAULT the two agree, so the reviewer admits the
    # bot's `@review` and a queued gate treats it as pending rather than
    # converging on the older verdict — the base behaviour, kept without a
    # coordinated caller update.
    comments = [verdict("i-am-marvin", "clean", T1, cid=1), comment(2, "claude[bot]", "@review", T2)]
    _, o, _ = run_trig(tmp_path, actor="claude[bot]", allowed_bots="claude[bot]")
    assert o["ok"] == "true"
    _, g, state = run_gate(tmp_path, comments, env_extra={"ALLOWED_BOTS": review_allowed_bots_default()})
    assert g["act"] == "skip" and lookups(state) == []


def test_review_stubs_route_bot_commenters_to_the_reusables_allow_list():
    # The stubs' association cost filter must not drop an allow-listed bot's
    # `@review` (a bot's association is NONE): a Bot commenter passes, and
    # the reusable's allowed_bots decides. Both comment branches, both stubs.
    clause = "github.event.comment.user.type == 'Bot'"
    assoc = "contains(fromJSON('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.comment.author_association)"
    for stub in (ROOT / ".github" / "workflows" / "claude-review-stub.yml",
                 ROOT / "examples" / "claude-review-stub.yml"):
        text = stub.read_text()
        assert text.count(clause) == 2 and text.count(assoc) == 2, stub


def test_allow_listed_bot_never_opens_the_fork_escape_hatch(tmp_path):
    _, o, _ = run_trig(tmp_path, actor="claude[bot]", allowed_bots="claude[bot]", head_repo="someone/r")
    assert o["ok"] == "false"


def test_fork_head_admits_only_a_trusted_commenter_sandboxed(tmp_path):
    _, o, _ = run_trig(tmp_path, actor="nobody", perms={"nobody": "read"}, head_repo="someone/r")
    assert o["ok"] == "false"
    _, o, _ = run_trig(tmp_path, actor="alice", perms={"alice": "write"}, head_repo="someone/r")
    assert o["ok"] == "true" and o["fork_head"] == "true"
    _, o, _ = run_trig(tmp_path, actor="i-am-marvin", head_repo="someone/r")
    assert o["ok"] == "true" and o["fork_head"] == "true"


def test_external_issue_requires_the_trusted_commenter(tmp_path):
    ibody = f"Upstream PR: {UPSTREAM}"
    _, o, _ = run_trig(tmp_path, actor="nobody", perms={"nobody": "read"}, is_pr=False, external=True, ibody=ibody)
    assert o["ok"] == "false"
    _, o, _ = run_trig(tmp_path, actor="alice", perms={"alice": "write"}, is_pr=False, external=True, ibody=ibody)
    assert o["ok"] == "true" and o["mode"] == "external" and o["upstream"] == UPSTREAM
    _, o, _ = run_trig(tmp_path, actor="i-am-marvin", is_pr=False, external=True, ibody=ibody)
    assert o["ok"] == "true" and o["mode"] == "external"
