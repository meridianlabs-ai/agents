"""Tests for the CI-fix loop's run-to-PR binding (Claude Security 4628657).

The caller stub forwards `workflow_run.pull_requests[0].number`, which
GitHub documents as an association — the open PRs matching the run's head,
"not necessarily ... pull requests that triggered the run" — so the reusable
workflow binds the failed run to its PR itself: the `bind-ci-run`
composite's step is lifted out of the action (the way the gate tests lift
theirs) and run here against a stub `gh` that answers the two API reads it
makes — the run's record and the repository's pull requests from the run's
head branch — from fixtures, one case per rule:

- the reusable acts only on the workflow_run event's own run, read back from
  the API: this repository's own `pull_request` run, same-repo head, on the
  forwarded branch, latest attempt the one that failed;
- the candidate origins are the same-repo PRs from that branch that were
  open when the run was created; exactly one binds (both association
  orders, same SHA / different base, a PR closed after the run started,
  fork heads and same-owner forks in the listing, a paginated listing);
- the bound PR must be the caller's number (an association from another
  repository refuses), open, at the run's head SHA (a head that advanced
  while queued refuses);
- the base the run tested is the PR's base with no retarget recorded on its
  timeline at or after the run's creation (a `base_ref_changed` or GitHub's
  `automatic_base_change_succeeded` event refuses); the fix job's sync pins
  that base (`sync-branch`'s `base` input, lifted and run here against a
  local origin);
- the run's actors (`actor`, and `triggering_actor` when different) must be
  a trusted login or hold write access — the model actions' own actor rule
  moved into trusted control flow, for both engines, before any write and
  again before landing; it is not a PR-author rule (the trusted-labeler
  policy is unchanged);
- with `revalidate`, the gate's context must be reproduced before landing;
- a persistent API error fails the step rather than binding.

The fix job has no launch signal at all: the Claude action's own
`execution_file` output is published by its error handler from whatever
file sits at the default path (which the PR's provisioning step can
pre-create), so a failed Claude step lands nothing and is refunded on the
step's outcome alone — checked structurally here, behaviourally in
test_ci_fix_composer.py.

Every fixture is synthetic: no case here reproduces GitHub's event
generation, the contents or order of a real completed-event association
array, or a hosted run. What the tests prove is what the enforcing shell
does with each input, including the inputs the scanner's scenario would
produce if GitHub produced them.

Also the workflow wiring the binding depends on (structural, on the YAML
text): the gate and land jobs pass the event's own run id and attempt, the
Land step is gated on the revalidation, emit-landing goes read-only on a
Claude step that failed without launching, and the example stub forwards
the event's own fields.
"""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github" / "actions" / "bind-ci-run" / "action.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "claude-auto.yml"
EXAMPLE = ROOT / "examples" / "claude-auto-stub.yml"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ci_fix_gate import TRUSTED_LOGINS, step_script  # noqa: E402
from test_land_helpers import sh, step_block, job_block  # noqa: E402

BIND = step_script(ACTION, "    - id: bind", 8)

SHA_A = "a" * 40      # the head the run tested
SHA_B = "b" * 40      # a later push
SHA_BASE = "c" * 40   # the base branch's tip when the gate reads it
RUN_CREATED = "2026-09-20T10:00:00Z"
BEFORE = "2026-09-20T09:00:00Z"
AFTER = "2026-09-20T10:05:00Z"

# A stub `gh` answering the two reads from fixture files in $STUB and logging
# every call. The run read 404s without a `run` fixture (a foreign or deleted
# run); a `run-fail` fixture makes it fail like a 5xx (an HTML body) on
# every attempt; the pull request listing is the `pulls` fixture verbatim
# (one or more JSON arrays, as `--paginate` prints pages), `pulls-fail` fails
# it the same way; the bound PR's timeline is the `timeline` fixture (empty
# without one); a collaborator permission is the `perm.<login>` fixture's
# text, a 404 without one. Anything else is unexpected.
GH_STUB = r'''#!/bin/bash
printf '%s\n' "$*" >>"$STUB/calls"
case "$1 $2" in
  api\ repos/o/r/actions/runs/*)
    if [ -f "$STUB/run-fail" ]; then echo '<html>502</html>'; exit 1; fi
    if [ -f "$STUB/run" ]; then cat "$STUB/run"; else echo '{"message":"Not Found"}'; exit 1; fi ;;
  api\ repos/o/r/pulls\?*)
    if [ -f "$STUB/pulls-fail" ]; then echo '<html>502</html>'; exit 1; fi
    if [ -f "$STUB/pulls" ]; then cat "$STUB/pulls"; else echo '[]'; fi ;;
  api\ repos/o/r/issues/*/timeline\?*)
    if [ -f "$STUB/timeline-fail" ]; then echo '<html>502</html>'; exit 1; fi
    if [ -f "$STUB/timeline" ]; then cat "$STUB/timeline"; else echo '[]'; fi ;;
  pr\ view)
    if [ -f "$STUB/pr" ]; then cat "$STUB/pr"; else echo '{"message":"Not Found"}'; exit 1; fi ;;
  api\ repos/o/r/branches/*)
    if [ -f "$STUB/branch-fail" ]; then echo '<html>502</html>'; exit 1; fi
    if [ ! -f "$STUB/branch-sha" ]; then echo '{"message":"Not Found"}'; exit 1; fi
    case "$*" in
      *--jq*) printf '%s' "$(cat "$STUB/branch-sha")" ;;
      *) printf '{"commit":{"sha":"%s"}}' "$(cat "$STUB/branch-sha")" ;;
    esac ;;
  api\ repos/o/r/collaborators/*)
    login=${2#repos/o/r/collaborators/}; login=${login%/permission}
    if [ -f "$STUB/perm.$login" ]; then printf '{"permission":"%s","role_name":"%s"}' "$(cat "$STUB/perm.$login")" "$(cat "$STUB/perm.$login")"
    else echo '{"message":"Not Found"}'; exit 1; fi ;;
  *) echo "unexpected gh $*" >&2; exit 2 ;;
esac
'''


def run_json(*, id=9002, repo="o/r", head_repo="o/r", event="pull_request", conclusion="failure",
             branch="shared", sha=SHA_A, attempt=1, created=RUN_CREATED, workflow_id=77,
             actor="alice", triggering_actor=None, pull_requests=()):
    return json.dumps({
        "id": id, "workflow_id": workflow_id, "event": event, "conclusion": conclusion,
        "head_branch": branch, "head_sha": sha, "run_attempt": attempt, "created_at": created,
        "repository": {"full_name": repo},
        "head_repository": {"full_name": head_repo} if head_repo is not None else None,
        "actor": {"login": actor}, "triggering_actor": {"login": triggering_actor or actor},
        "pull_requests": [{"number": n, "base": {"ref": b, "repo": {"name": "r"}}} for n, b in pull_requests],
    })


def pr(number, *, state="open", created=BEFORE, closed=None, head_repo="o/r", head_ref="shared",
       head_sha=SHA_A, base_repo="o/r", base_ref="main"):
    return {"number": number, "state": state, "created_at": created, "closed_at": closed,
            "head": {"ref": head_ref, "sha": head_sha, "repo": {"full_name": head_repo}},
            "base": {"ref": base_ref, "sha": "c" * 40, "repo": {"full_name": base_repo}},
            "labels": [{"name": "auto"}], "user": {"login": "someone"}}


def pages(*page_lists):
    return "\n".join(json.dumps(list(p)) for p in page_lists) + "\n"


def bind(tmp_path, *, run=None, pulls=None, expect_pr="101", run_id="9002", event_run_id="9002",
         event_attempt="1", event_name="workflow_run", head="shared", revalidate=None, fixtures=None,
         timeline=None, perms=None, trusted=TRUSTED_LOGINS, base_sha=SHA_BASE):
    """Run the lifted step under the composite's shell options (`bash
    --noprofile --norc -eo pipefail`), with the fixtures given; `revalidate`
    is the gate's (head_sha, base_ref, run_attempt) to reproduce; `perms`
    maps logins to their collaborator permission (the default run actor
    `alice` holds write unless a case says otherwise)."""
    binp = tmp_path / "bin"
    binp.mkdir(exist_ok=True)
    for name, body in (("gh", GH_STUB), ("sleep", "#!/bin/bash\nexit 0\n")):
        f = binp / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    if run is not None:
        (stub / "run").write_text(run)
    if pulls is not None:
        (stub / "pulls").write_text(pulls if isinstance(pulls, str) else pages(pulls))
    if timeline is not None:
        (stub / "timeline").write_text(timeline if isinstance(timeline, str) else pages(timeline))
    if base_sha is not None:
        (stub / "branch-sha").write_text(base_sha)
    for login, perm in ({"alice": "write"} | (perms or {})).items():
        if perm is not None:
            (stub / f"perm.{login}").write_text(perm)
    for name, content in (fixtures or {}).items():
        (stub / name).write_text(content)
    (stub / "calls").write_text("")
    out = tmp_path / "output"
    out.write_text("")
    env = {"PATH": f"{binp}:{os.environ['PATH']}", "STUB": str(stub), "GITHUB_OUTPUT": str(out),
           "GH_TOKEN": "x", "REPO": "o/r", "RUN_ID": run_id, "EVENT_NAME": event_name,
           "EVENT_RUN_ID": event_run_id, "EVENT_RUN_ATTEMPT": event_attempt, "HEAD": head,
           "EXPECT_PR": expect_pr, "REVALIDATE": "true" if revalidate else "false",
           "EXPECT_HEAD_SHA": revalidate[0] if revalidate else "",
           "EXPECT_BASE": revalidate[1] if revalidate else "",
           "EXPECT_ATTEMPT": revalidate[2] if revalidate else "", "TRUSTED_LOGINS": trusted}
    res = sh("bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", BIND, check=False, env=env)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    calls = (stub / "calls").read_text().splitlines() if (stub / "calls").exists() else []
    return res, outputs, calls


def refused(res, out, fragment):
    assert res.returncode == 0, res.stderr
    assert out["ok"] == "" and "pr" not in out, out
    assert fragment in out["reason"], out["reason"]
    assert out["reason"] in res.stdout


def run_reads(calls):
    return [c for c in calls if c.startswith("api repos/o/r/actions/runs/")]


def pull_lists(calls):
    return [c for c in calls if c.startswith("api repos/o/r/pulls?")]


def timeline_reads(calls):
    return [c for c in calls if "/timeline?" in c]


def lookups(calls, login=None):
    return [c for c in calls if c.startswith("api repos/o/r/collaborators/")
            and (login is None or f"/{login}/" in c)]


def retarget(at):
    return {"event": "base_ref_changed", "created_at": at, "actor": {"login": "someone"}}


# --- the happy path ----------------------------------------------------------


def test_binds_the_only_same_repo_pr_open_on_the_branch_when_the_run_was_created(tmp_path):
    res, out, calls = bind(tmp_path, run=run_json(pull_requests=[(101, "main")]), pulls=[pr(101)])
    assert res.returncode == 0, res.stderr
    assert out["ok"] == "1" and out["reason"] == ""
    assert out["pr"] == "101" and out["head_sha"] == SHA_A and out["head_branch"] == "shared"
    assert out["base_ref"] == "main" and out["base_sha"] == SHA_BASE
    assert out["run_attempt"] == "1" and out["workflow_id"] == "77"
    assert out["created_at"] == RUN_CREATED and out["actor"] == "alice" and out["triggering_actor"] == "alice"
    # One run read, one permission lookup for the actor, one enumeration of
    # this repo's PRs from the branch, by the owner-qualified head filter,
    # paginated, one read of the bound PR's timeline. Never `gh pr view` or
    # a branch-name lookup that picks one PR.
    assert len(run_reads(calls)) == 1 and pull_lists(calls) == [
        "api repos/o/r/pulls?state=all&per_page=100&head=o:shared --paginate"]
    assert lookups(calls) == ["api repos/o/r/collaborators/alice/permission"]
    assert timeline_reads(calls) == ["api repos/o/r/issues/101/timeline?per_page=100 --paginate"]
    assert "api repos/o/r/branches/main" in calls
    assert len(calls) == 5
    assert f"bound to PR #101 (shared at {SHA_A} -> main, now at {SHA_BASE})" in res.stdout


def test_the_head_branch_is_uri_encoded_in_the_listing(tmp_path):
    res, out, calls = bind(tmp_path, run=run_json(branch="claude/issue-1 fix"), head="claude/issue-1 fix",
                           pulls=[pr(101, head_ref="claude/issue-1 fix")])
    assert res.returncode == 0, res.stderr
    assert out["ok"] == "1"
    assert pull_lists(calls) == ["api repos/o/r/pulls?state=all&per_page=100&head=o:claude%2Fissue-1%20fix --paginate"]


# --- the candidate set: which PRs can have been the origin --------------------


@pytest.mark.parametrize("expect_pr", ["101", "102"])
def test_two_prs_sharing_the_head_refuse_whichever_the_association_named_first(tmp_path, expect_pr):
    # The scanner's topology (observed once on the fork, PRs 314/315): one
    # branch into two bases. Both PRs' runs have the same head SHA, branch
    # and event; nothing the API exposes tells them apart, and `[0]` is
    # whichever GitHub listed first — modelled here by the number the caller
    # forwards, since no fixture can reproduce GitHub's ordering. Neither
    # order binds: no counter, stage, sync or model work follows.
    res, out, calls = bind(tmp_path, expect_pr=expect_pr,
                           run=run_json(pull_requests=[(101, "main"), (102, "alternate")]),
                           pulls=[pr(101, base_ref="main"), pr(102, base_ref="alternate")])
    refused(res, out, "cannot be bound to one PR: #101 (open, base main) and #102 (open, base alternate)")
    assert "Close the PR that should not share the branch (retargeting it keeps it a candidate), then push a new commit (re-running this run keeps its creation time)." in out["reason"]
    assert len(calls) == 3 and timeline_reads(calls) == []      # run, actor, listing; no PR chosen


def test_a_pr_closed_after_the_run_was_created_still_counts(tmp_path):
    # The surviving-association case: the other PR closed before the run
    # completed, so the completed event's list held only #101 — a singleton
    # that is not thereby the origin. #102 was open when the run was created
    # and could have triggered it, so the run stays ambiguous.
    res, out, _ = bind(tmp_path, run=run_json(pull_requests=[(101, "main")]),
                       pulls=[pr(101), pr(102, state="closed", closed=AFTER, base_ref="alternate")])
    refused(res, out, "#101 (open, base main) and #102 (closed, base alternate)")


def test_a_pr_closed_before_the_run_was_created_does_not_count(tmp_path):
    # ...whereas one closed before the run existed cannot have triggered it:
    # the legitimate PR's later failing run binds.
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101), pr(102, state="closed", closed=BEFORE)])
    assert res.returncode == 0, res.stderr
    assert out["ok"] == "1" and out["pr"] == "101"


def test_a_pr_opened_after_the_run_was_created_does_not_count(tmp_path):
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101), pr(102, created=AFTER)])
    assert res.returncode == 0, res.stderr
    assert out["ok"] == "1" and out["pr"] == "101"


def test_a_pr_created_or_closed_at_the_runs_own_second_counts(tmp_path):
    # Inclusive at both ends: second-precision timestamps make "the same
    # second" the fail-closed side.
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101), pr(102, created=RUN_CREATED)])
    refused(res, out, "cannot be bound to one PR")
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101), pr(102, state="closed", closed=RUN_CREATED)])
    refused(res, out, "cannot be bound to one PR")


def test_fork_heads_and_same_owner_forks_in_the_listing_are_not_candidates(tmp_path):
    # `head=o:shared` matches every head in the owner's repositories; only
    # THIS repository's heads are candidates (a fork PR named like the branch
    # was finding 4122320's problem, and an owner may hold a same-named fork).
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[
        pr(101), pr(103, head_repo="someone/r"), pr(104, head_repo="o/r-fork"), pr(105, head_ref="other")])
    assert res.returncode == 0, res.stderr
    assert out["ok"] == "1" and out["pr"] == "101"


def test_a_paginated_listing_is_flattened(tmp_path):
    res, out, _ = bind(tmp_path, run=run_json(), pulls=pages([pr(101)], [pr(102, base_ref="alternate")]))
    refused(res, out, "#101 (open, base main) and #102 (open, base alternate)")


def test_no_candidate_refuses(tmp_path):
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(102, state="closed", closed=BEFORE)])
    refused(res, out, "no same-repo PR from 'shared' was open when run 9002 was created")


# --- the bound PR against the caller's number, its state and its head ---------


def test_an_association_from_another_repository_refuses(tmp_path):
    # Observed on the fork (run 35355080785): the association list named an
    # UPSTREAM PR — a number that means a different PR in this repository.
    # The run binds to #101 here; the caller's 5470 disagrees, so nothing
    # runs against either number.
    res, out, _ = bind(tmp_path, expect_pr="5470", run=run_json(pull_requests=[(5470, "main")]), pulls=[pr(101)])
    refused(res, out, "named PR #5470, but run 9002 binds to PR #101")


def test_a_head_that_advanced_while_the_run_was_queued_refuses(tmp_path):
    # The run tested SHA_A; the PR's live head is SHA_B (a push since). The
    # newer push's own run supersedes this one; fixing SHA_B against SHA_A's
    # logs is never right.
    res, out, _ = bind(tmp_path, run=run_json(sha=SHA_A), pulls=[pr(101, head_sha=SHA_B)])
    refused(res, out, f"PR #101's head is now {SHA_B} but run 9002 tested {SHA_A}")


def test_a_bound_pr_that_closed_since_refuses(tmp_path):
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101, state="closed", closed=AFTER)])
    refused(res, out, "PR #101 is closed, not open")


# --- the run itself: this event's own, this repo's own, still the failure ------


def test_only_a_workflow_run_event_is_acted_on(tmp_path):
    res, out, calls = bind(tmp_path, run=run_json(), pulls=[pr(101)], event_name="issue_comment")
    refused(res, out, "acts only on a workflow_run event")
    assert calls == []


def test_ci_run_id_must_be_the_run_this_event_completed(tmp_path):
    res, out, calls = bind(tmp_path, run=run_json(), pulls=[pr(101)], run_id="9003")
    refused(res, out, "ci_run_id 9003 is not the run this event completed (9002)")
    assert calls == []
    for bad in ("", "abc", "0", "-1", "9002x"):
        res, out, calls = bind(tmp_path, run=run_json(), pulls=[pr(101)], run_id=bad, event_run_id=bad)
        refused(res, out, "is not a run id")
        assert calls == []


def test_no_pr_number_skips_before_any_read(tmp_path):
    for empty in ("", "null", "abc"):
        res, out, calls = bind(tmp_path, run=run_json(), pulls=[pr(101)], expect_pr=empty)
        refused(res, out, "No PR number from the triggering run")
        assert calls == []


def test_a_foreign_run_refuses(tmp_path):
    # Not found in this repo (the id belongs to another repository, or was
    # deleted): a refusal, not a failed step.
    res, out, calls = bind(tmp_path, pulls=[pr(101)])
    refused(res, out, "run 9002 is not a run of o/r (not found)")
    assert len(run_reads(calls)) == 1 and pull_lists(calls) == []
    # Found but recorded against another repository.
    res, out, _ = bind(tmp_path, run=run_json(repo="o/other"), pulls=[pr(101)])
    refused(res, out, "run 9002 belongs to 'o/other', not o/r")


def test_a_fork_head_run_refuses(tmp_path):
    res, out, _ = bind(tmp_path, run=run_json(head_repo="someone/r"), pulls=[pr(101)])
    refused(res, out, "head is in 'someone/r' (a fork)")
    res, out, _ = bind(tmp_path, run=run_json(head_repo=None), pulls=[pr(101)])
    refused(res, out, "(a fork)")


def test_a_run_that_is_not_a_pull_request_run_refuses(tmp_path):
    # A `push` run on the same branch has the same head; it is not a PR's.
    res, out, _ = bind(tmp_path, run=run_json(event="push"), pulls=[pr(101)])
    refused(res, out, "is a 'push' run, not a pull_request run")


def test_a_run_on_another_branch_than_the_caller_forwarded_refuses(tmp_path):
    res, out, _ = bind(tmp_path, run=run_json(branch="other"), pulls=[pr(101)])
    refused(res, out, "ran on branch 'other', not 'shared'")


def test_a_rerun_since_the_event_makes_it_stale(tmp_path):
    # The event completed attempt 1; someone re-ran the failed jobs, so the
    # run's latest attempt is 2 (in progress, or already green). The
    # re-run's own completion event decides; this one does nothing.
    res, out, _ = bind(tmp_path, run=run_json(attempt=2, conclusion=None, triggering_actor="bob"),
                       pulls=[pr(101)], perms={"bob": "write"})
    refused(res, out, "has moved on to attempt 2 since attempt 1 completed")


def test_a_latest_attempt_that_did_not_fail_refuses(tmp_path):
    for conclusion in ("success", "cancelled", None):
        res, out, _ = bind(tmp_path, run=run_json(conclusion=conclusion), pulls=[pr(101)])
        refused(res, out, "not failure; nothing to fix")


def test_a_run_without_a_usable_head_sha_or_creation_time_refuses(tmp_path):
    res, out, _ = bind(tmp_path, run=run_json(sha="not-a-sha"), pulls=[pr(101)])
    refused(res, out, "records no head SHA")
    res, out, _ = bind(tmp_path, run=run_json(created="yesterday"), pulls=[pr(101)])
    refused(res, out, "records no creation time")


# --- the run's actors (B2: authorization in trusted control flow) -------------


def test_a_run_started_by_a_read_only_account_refuses_before_any_pr_read(tmp_path):
    # The reduced-privilege case the investigation kept open: an organization
    # member with read access opens a same-repo PR from a writer's branch;
    # the `opened` run's actor is that member. The engines' own actor
    # checks refused a read-only human later — after the counter, the stage
    # move and the base merge. Now the gate refuses it first, and the land
    # job would again. The PR is never even enumerated.
    res, out, calls = bind(tmp_path, run=run_json(actor="read-only-member"), pulls=[pr(101)],
                           perms={"read-only-member": "read"})
    refused(res, out, "was started by 'read-only-member', who is not a trusted login and holds no write access")
    assert pull_lists(calls) == [] and timeline_reads(calls) == []
    assert lookups(calls) == ["api repos/o/r/collaborators/read-only-member/permission"]


def test_a_write_access_actor_passes_and_is_looked_up_once(tmp_path):
    for perm in ("write", "maintain", "admin"):
        res, out, calls = bind(tmp_path, run=run_json(actor="bob", triggering_actor="bob"), pulls=[pr(101)],
                               perms={"bob": perm})
        assert res.returncode == 0, res.stderr
        assert out["ok"] == "1" and out["actor"] == "bob" and out["triggering_actor"] == "bob", perm
        assert lookups(calls) == ["api repos/o/r/collaborators/bob/permission"], perm


def test_a_trusted_login_passes_without_a_lookup_and_other_bots_are_refused(tmp_path):
    # The machine account's pushes (the land job's) re-run CI as its bot
    # login; that is the loop's own next round. Any other App is refused
    # without a lookup (the endpoint 404s for Apps; github-actions[bot] is
    # every repository's workflow) — stricter than either engine's own check
    # (the Claude action admits any [bot]; the Codex step named claude), by
    # decision (Ransom, 2026-09-22), applied to both engines before any write.
    for login in TRUSTED_LOGINS.split(","):
        res, out, calls = bind(tmp_path, run=run_json(actor=login), pulls=[pr(101)])
        assert res.returncode == 0, res.stderr
        assert out["ok"] == "1" and lookups(calls) == [], login
    for bot in ("github-actions[bot]", "claude[bot]", "dependabot[bot]"):
        res, out, calls = bind(tmp_path, run=run_json(actor=bot), pulls=[pr(101)], perms={bot: "admin"})
        refused(res, out, f"was started by '{bot}'")
        assert lookups(calls) == [], bot


def test_a_rerun_by_an_unauthorized_account_refuses_even_after_a_trusted_start(tmp_path):
    # Both actors are judged when they differ: the writer's push started
    # attempt 1; a read-only member re-ran it (attempt 2's event). Claude's
    # action checks both too; Codex checks only the current actor — the
    # gate now decides for both engines.
    res, out, calls = bind(tmp_path, run=run_json(actor="alice", triggering_actor="read-only-member", attempt=2),
                           event_attempt="2", pulls=[pr(101)], perms={"read-only-member": "read"})
    refused(res, out, "was re-run by 'read-only-member'")
    assert len(lookups(calls)) == 2
    res, out, _ = bind(tmp_path, run=run_json(actor="alice", triggering_actor="bob", attempt=2),
                       event_attempt="2", pulls=[pr(101)], perms={"bob": "write"})
    assert res.returncode == 0 and out["ok"] == "1" and out["triggering_actor"] == "bob"


def test_a_failed_or_missing_permission_lookup_refuses(tmp_path):
    # No fixture: the endpoint 404s (a deleted account) through the retry.
    res, out, calls = bind(tmp_path, run=run_json(actor="ghost"), pulls=[pr(101)])
    refused(res, out, "was started by 'ghost'")
    assert len(lookups(calls, "ghost")) == 1          # a definite Not Found is not retried
    res, out, _ = bind(tmp_path, run=run_json(actor=""), pulls=[pr(101)])
    refused(res, out, "records no actor")


def test_the_actor_rule_is_not_a_pr_author_rule(tmp_path):
    # A maintainer labelled a read-only member's PR on purpose; the runs
    # that drive it come from the maintainer's or the machine account's
    # pushes to the branch, and those actors pass. Nothing here reads the
    # PR's author (`user.login` is in the fixture and is never consulted).
    res, out, calls = bind(tmp_path, run=run_json(actor="alice"), pulls=[pr(101)])
    assert res.returncode == 0, res.stderr
    assert out["ok"] == "1" and lookups(calls) == ["api repos/o/r/collaborators/alice/permission"]


# --- the base the run tested (B1: a retarget since the run refuses) ------------


def test_a_pr_retargeted_after_the_run_was_created_refuses(tmp_path):
    # The run tested a merge into `release`; the PR now targets `main` with
    # the same head. The current base passes every current-metadata check —
    # so the PR's timeline is the authority: a `base_ref_changed` event at
    # or after the run's creation refuses, at the gate and at landing alike.
    res, out, calls = bind(tmp_path, run=run_json(pull_requests=[(101, "release")]),
                           pulls=[pr(101, base_ref="main")], timeline=[retarget(AFTER)])
    refused(res, out, f"PR #101 was retargeted at {AFTER}, after run 9002 was created ({RUN_CREATED})")
    assert timeline_reads(calls) == ["api repos/o/r/issues/101/timeline?per_page=100 --paginate"]
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101, base_ref="main")],
                       timeline=[retarget(RUN_CREATED)])
    refused(res, out, "was retargeted at " + RUN_CREATED)
    # Revalidation: retargeted between the gate and the landing (the gate
    # saw `main`; the PR was retargeted to `release` and back to `main`, or
    # the base differs outright — either refuses).
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101, base_ref="main")],
                       timeline=[retarget(AFTER)], revalidate=(SHA_A, "main", "1"))
    refused(res, out, "was retargeted at " + AFTER)


def test_a_retarget_before_the_run_does_not_matter_and_github_auto_retargets_count(tmp_path):
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101)], timeline=[retarget(BEFORE)])
    assert res.returncode == 0, res.stderr
    assert out["ok"] == "1" and out["base_ref"] == "main"
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101)], timeline=[
        {"event": "automatic_base_change_succeeded", "created_at": AFTER},
        {"event": "labeled", "created_at": AFTER, "label": {"name": "auto"}}])
    refused(res, out, "was retargeted at " + AFTER)


def test_a_paginated_timeline_is_flattened_and_a_persistent_timeline_error_fails(tmp_path):
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101)],
                       timeline=pages([{"event": "committed", "created_at": BEFORE}], [retarget(AFTER)]))
    refused(res, out, "was retargeted at " + AFTER)
    res, out, calls = bind(tmp_path, run=run_json(), pulls=[pr(101)], fixtures={"timeline-fail": ""})
    assert res.returncode != 0 and "ok" not in out
    assert len(timeline_reads(calls)) == 4


def test_a_run_without_a_workflow_id_refuses(tmp_path):
    res, out, _ = bind(tmp_path, run=run_json(workflow_id=None), pulls=[pr(101)])
    refused(res, out, "records no workflow id")
    res, out, _ = bind(tmp_path, run=run_json(workflow_id="abc"), pulls=[pr(101)])
    refused(res, out, "records no workflow id")


def test_the_association_list_is_logged_as_information_only(tmp_path):
    res, out, _ = bind(tmp_path, run=run_json(pull_requests=[(101, "main"), (5470, "main")]), pulls=[pr(101)])
    assert res.returncode == 0, res.stderr
    assert out["ok"] == "1"
    assert "associated PRs (informational — GitHub lists the open PRs matching the head, not the trigger): r#101 (base main), r#5470 (base main)." in res.stdout


# --- API failures fail the step; they never bind -------------------------------


def test_a_persistent_run_read_error_fails_the_step(tmp_path):
    res, out, calls = bind(tmp_path, pulls=[pr(101)], fixtures={"run-fail": ""})
    assert res.returncode != 0
    assert "ok" not in out
    assert len(run_reads(calls)) == 4 and pull_lists(calls) == []       # four attempts, as ghr


def test_a_persistent_listing_error_fails_the_step(tmp_path):
    res, out, calls = bind(tmp_path, run=run_json(), fixtures={"pulls-fail": ""})
    assert res.returncode != 0
    assert "ok" not in out
    assert len(pull_lists(calls)) == 4


# --- revalidation before landing -----------------------------------------------


def test_revalidation_reproduces_the_gates_context(tmp_path):
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101)], revalidate=(SHA_A, "main", "1"))
    assert res.returncode == 0, res.stderr
    assert out["ok"] == "1" and out["pr"] == "101"


def test_revalidation_refuses_a_retargeted_pr(tmp_path):
    # The runner merged `main` into the branch; landing a merge of the wrong
    # base against a PR now targeting `release` is refused.
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101, base_ref="release")],
                       revalidate=(SHA_A, "main", "1"))
    refused(res, out, "base changed from 'main' to 'release' during the round")


def test_revalidation_refuses_a_branch_pushed_during_the_round(tmp_path):
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101, head_sha=SHA_B)], revalidate=(SHA_A, "main", "1"))
    refused(res, out, f"PR #101's head is now {SHA_B} but run 9002 tested {SHA_A}")


def test_revalidation_refuses_a_rerun_and_a_reopened_second_pr(tmp_path):
    res, out, _ = bind(tmp_path, run=run_json(attempt=2), pulls=[pr(101)], revalidate=(SHA_A, "main", "1"))
    refused(res, out, "has moved on to attempt 2")
    # A PR from the branch that was closed when the run was created but has
    # been reopened since reads as open with its original creation time, so
    # it joins the candidates: the fail-closed side.
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101), pr(102, base_ref="alternate")],
                       revalidate=(SHA_A, "main", "1"))
    refused(res, out, "cannot be bound to one PR")


def test_revalidation_refuses_when_the_gates_context_is_missing(tmp_path):
    for ctx in (("", "main", "1"), (SHA_A, "", "1"), (SHA_A, "main", ""), ("nope", "main", "1")):
        res, out, calls = bind(tmp_path, run=run_json(), pulls=[pr(101)], revalidate=ctx)
        refused(res, out, "revalidation was asked for without the gate's context")
        assert calls == [], ctx


def test_revalidation_refuses_a_gate_head_sha_the_run_does_not_carry(tmp_path):
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101)], revalidate=(SHA_B, "main", "1"))
    refused(res, out, f"head SHA read {SHA_B} at the gate and {SHA_A} now")


# --- the fix job has no launch signal (B2: nothing it can observe proves one) -----


def test_the_fix_job_keys_landing_and_refund_on_step_outcomes_not_on_execution_files():
    """The action's `execution_file` output is published by its own error
    handler from whatever file sits at the default path (review round 2:
    `setExecutionFileOutputIfPresent()` runs in the catch block), which the
    PR's provisioning step can pre-create — so no "did the agent launch"
    signal exists that the fix job could trust. The landing composer and
    emit-landing withhold on the Claude step's OUTCOME being `failure`, and
    the refund keys on the agent outcome and the push only."""
    text = WORKFLOW.read_text()
    assert "id: launched" not in text and "agent_started" not in text and "AGENT_STARTED" not in text
    # One job per engine since 2026-09-22: the Claude job's composer and
    # emit withhold on the Claude step's outcome, the codex job's on its
    # unresolved-merge guard.
    claude_job, codex_job = job_block(text, "fix"), job_block(text, "fix-codex")
    assert 'if [ "${CLAUDE_OUTCOME:-}" = "failure" ]; then' in step_block(claude_job, "landing")
    assert 'if [ "${CODEXGUARD_OUTCOME:-}" != "success" ]; then' in step_block(codex_job, "landing")
    assert "read-only: ${{ steps.claude.outcome == 'failure' && 'true' || 'false' }}" in claude_job
    assert "read-only: ${{ steps.codexguard.outcome != 'success' && 'true' || 'false' }}" in codex_job
    assert "      agent_outcome: ${{ steps.claude.outcome }}\n" in claude_job
    assert ("      agent_outcome: ${{ (steps.codexprep.outcome == 'failure' || steps.codexuser.outcome == 'failure' || "
            "steps.codexhome.outcome == 'failure' || steps.codexcompose.outcome == 'failure' || "
            "steps.codexfix.outcome == 'failure') && 'failure' || steps.codexfix.outcome }}\n") in codex_job
    land = job_block(text, "land")
    assert ("      AGENT_OUTCOME: ${{ needs.gate.outputs.engine == 'codex' && needs.fix-codex.outputs.agent_outcome "
            "|| needs.fix.outputs.agent_outcome }}\n") in land
    refund = text[text.index("- name: Refund infra-crashed attempt"):]
    refund = refund[:refund.index("run: |")]
    assert "env.AGENT_OUTCOME != 'success' &&" in refund
    assert "steps.land.outputs.pushed != '1'" in refund
    assert "execution" not in refund


# --- the base tip the sync must merge (B1: pinned gate -> merge) ------------------


def test_the_base_tip_is_read_from_the_branch_and_emitted(tmp_path):
    # From the branch, not the PR object's `base.sha` (the tip as of the
    # PR's last update): the sync is pinned to exactly this commit.
    res, out, calls = bind(tmp_path, run=run_json(), pulls=[pr(101)], base_sha="d" * 40)
    assert res.returncode == 0, res.stderr
    assert out["ok"] == "1" and out["base_sha"] == "d" * 40
    assert calls[-1] == "api repos/o/r/branches/main"


def test_an_unreadable_or_malformed_base_tip_refuses_and_a_persistent_error_fails(tmp_path):
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101)], base_sha=None)
    refused(res, out, "base branch 'main' cannot be read (not found)")
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101)], base_sha="not-a-sha")
    refused(res, out, "has no readable tip ('not-a-sha')")
    res, out, calls = bind(tmp_path, run=run_json(), pulls=[pr(101)], fixtures={"branch-fail": ""})
    assert res.returncode != 0 and "ok" not in out
    assert len([c for c in calls if c.startswith("api repos/o/r/branches/")]) == 4


def test_revalidation_does_not_require_the_base_tip_to_stand_still(tmp_path):
    # The base may advance once the merge is made (other PRs land); the
    # landing's own ancestry rules govern the push. What revalidation
    # requires is the PR, head SHA, base BRANCH and attempt.
    res, out, _ = bind(tmp_path, run=run_json(), pulls=[pr(101)], base_sha="d" * 40,
                       revalidate=(SHA_A, "main", "1"))
    assert res.returncode == 0, res.stderr
    assert out["ok"] == "1" and out["base_sha"] == "d" * 40


# --- sync-branch pins the base the gate established (B1) -------------------------

SYNC_ACTION = ROOT / ".github" / "actions" / "sync-branch" / "action.yml"
SYNC = step_script(SYNC_ACTION, "    - id: sync", 8)


def sync_repo(tmp_path):
    """A local origin with `main`, `release` and the PR branch `shared`
    (cut from main), and a checkout of `shared` with origin pointing at it —
    the CI-fix loop's shape (checkout=false: the head is already checked out)."""
    from test_land_helpers import git
    tmp_path.mkdir(exist_ok=True)
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    git("init", "-q", "-b", "main", str(work), cwd=tmp_path)
    git("config", "user.email", "a@b", cwd=work)
    git("config", "user.name", "a", cwd=work)
    (work / "f").write_text("1\n")
    git("add", "f", cwd=work)
    git("commit", "-qm", "base", cwd=work)
    git("checkout", "-qb", "shared", cwd=work)
    (work / "g").write_text("1\n")
    git("add", "g", cwd=work)
    git("commit", "-qm", "head", cwd=work)
    git("checkout", "-q", "main", cwd=work)
    (work / "f").write_text("2\n")
    git("commit", "-qam", "main moved", cwd=work)
    git("checkout", "-qb", "release", "main~1", cwd=work)
    (work / "r").write_text("1\n")
    git("add", "r", cwd=work)
    git("commit", "-qm", "release moved", cwd=work)
    git("remote", "add", "origin", str(origin), cwd=work)
    git("push", "-q", "origin", "main", "release", "shared", cwd=work)
    git("checkout", "-q", "shared", cwd=work)
    return work


def sync(tmp_path, *, live_base, pinned_base, pinned_base_sha=""):
    """`pinned_base_sha="TIP"` pins to this fixture's own origin/<live_base>
    tip (every fixture repo has its own SHAs)."""
    work = sync_repo(tmp_path)
    if pinned_base_sha == "TIP":
        pinned_base_sha = origin_tip(work, live_base)
    binp = tmp_path / "bin"
    binp.mkdir(exist_ok=True)
    gh = binp / "gh"
    gh.write_text(GH_STUB)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    (stub / "calls").write_text("")
    (stub / "pr").write_text(json.dumps({"headRefName": "shared", "baseRefName": live_base,
                                         "isCrossRepository": False, "state": "OPEN"}))
    (stub / "branch-sha").write_text(SHA_A)
    out = tmp_path / "output"
    out.write_text("")
    env = {"PATH": f"{binp}:{os.environ['PATH']}", "STUB": str(stub), "GITHUB_OUTPUT": str(out),
           "GH_TOKEN": "x", "REPO": "o/r", "NUM": "101", "ENGINE": "claude", "CHECKOUT": "false",
           "USER_NAME": "a", "USER_EMAIL": "a@b", "PINNED_BASE": pinned_base, "PINNED_BASE_SHA": pinned_base_sha,
           "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_ALLOW_PROTOCOL": "file"}
    res = sh("bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", SYNC, cwd=work, check=False, env=env)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    return res, outputs, work


def origin_tip(work, branch):
    from test_land_helpers import git
    return git("rev-parse", f"origin/{branch}", cwd=work).stdout.strip()


def test_sync_merges_exactly_the_pinned_base_tip(tmp_path):
    # Two fixtures of the same shape: the tip the gate read is the one on
    # origin — merged; a different one — the base moved between the gate
    # and the sync — is refused after the fetch and before the merge, with
    # HEAD and the tree untouched.
    from test_land_helpers import git
    res, out, work = sync(tmp_path / "ok", live_base="main", pinned_base="main", pinned_base_sha="TIP")
    assert res.returncode == 0, res.stderr
    assert out["merge_sha"] and (work / "f").read_text() == "2\n"
    res, out, work = sync(tmp_path / "moved", live_base="main", pinned_base="main", pinned_base_sha="e" * 40)
    assert res.returncode != 0
    tip = origin_tip(work, "main")
    assert f"origin/main is at {tip}, but the caller's trusted context read {'e' * 40}" in res.stdout
    assert "merge_sha" not in out and (work / "f").read_text() == "1\n"
    assert git("rev-parse", "HEAD", cwd=work).stdout == git("rev-parse", "origin/shared", cwd=work).stdout
    assert out["start_sha"]                                  # the pin is checked after the fetch, before the merge


def test_sync_merges_the_pinned_base_when_the_pr_still_targets_it(tmp_path):
    res, out, work = sync(tmp_path, live_base="main", pinned_base="main")
    assert res.returncode == 0, res.stderr
    assert out["base"] == "main" and out["merge_sha"]
    assert (work / "f").read_text() == "2\n" and not (work / "r").exists()


def test_sync_refuses_a_pr_retargeted_since_the_gate_before_fetching_or_merging(tmp_path):
    # The gate bound the run to a merge into `main`; the PR now targets
    # `release`. Nothing is fetched or merged: the step fails, the agent is
    # skipped and the attempt refunded.
    res, out, work = sync(tmp_path, live_base="release", pinned_base="main")
    assert res.returncode != 0
    assert "established 'main' (the base the failed run tested); refusing to merge a base the run never saw" in res.stdout
    assert "base" not in out and "merge_sha" not in out
    assert (work / "f").read_text() == "1\n" and not (work / "r").exists()
    from test_land_helpers import git
    assert git("rev-parse", "HEAD", cwd=work).stdout == git("rev-parse", "origin/shared", cwd=work).stdout
    assert git("status", "--porcelain", cwd=work).stdout == ""


def test_sync_without_a_pinned_base_keeps_the_live_base(tmp_path):
    # The other callers (claude.yml, claude-auto-review.yml) pass no base:
    # unchanged behaviour.
    res, out, work = sync(tmp_path, live_base="release", pinned_base="")
    assert res.returncode == 0, res.stderr
    assert out["base"] == "release" and (work / "r").exists()


# --- the wiring in claude-auto.yml and the example stub ------------------------


def test_gate_and_land_pass_the_events_own_run_to_the_binding():
    text = WORKFLOW.read_text()
    for step_id in ("bind", "rebind"):
        block = step_block(text, step_id)
        assert "uses: meridianlabs-ai/agents/.github/actions/bind-ci-run@main" in block
        assert "run-id: ${{ inputs.ci_run_id }}" in block
        assert "event-name: ${{ github.event_name }}" in block
        assert "event-run-id: ${{ github.event.workflow_run.id }}" in block
        assert "event-run-attempt: ${{ github.event.workflow_run.run_attempt }}" in block
        assert "expect-head-branch: ${{ inputs.head_branch }}" in block
    assert "expect-pr: ${{ inputs.pr_number }}" in step_block(text, "bind")
    for step_id in ("bind", "rebind"):
        assert "trusted-logins: ${{ env.TRUSTED_LOGINS }}" in step_block(text, step_id)
    # The base the gate established reaches the sync (B1) and the mints
    # carry the run read's permission (B3; the exact sets are pinned in
    # test_app_token_minting.py).
    assert "base: ${{ needs.gate.outputs.base_ref }}" in step_block(text, "sync")
    assert "base-sha: ${{ needs.gate.outputs.base_sha }}" in step_block(text, "sync")
    assert "      base_sha: ${{ steps.bind.outputs.base_sha }}" in text
    assert text.count("          permission-actions: read\n") == 2
    rebind = step_block(text, "rebind")
    assert 'revalidate: "true"' in rebind
    assert "expect-pr: ${{ needs.gate.outputs.pr }}" in rebind
    for name in ("head_sha", "base_ref", "run_attempt"):
        assert f"expect-{name.replace('_', '-')}: ${{{{ needs.gate.outputs.{name} }}}}" in rebind
        assert f"      {name}: ${{{{ steps.bind.outputs.{name} }}}}" in text     # the gate job's outputs


def test_the_gate_resolves_the_bound_pr_and_the_land_step_waits_for_the_revalidation():
    text = WORKFLOW.read_text()
    resolve = step_block(text, "resolve")
    assert "PR_NUMBER: ${{ steps.bind.outputs.pr }}" in resolve
    assert "BIND_OK: ${{ steps.bind.outputs.ok }}" in resolve
    assert "PR_NUMBER: ${{ inputs.pr_number }}" not in resolve
    land = step_block(text, "land")
    assert "steps.rebind.outputs.ok == '1'" in land.split("uses:")[0]
    assert "pr-number: ${{ needs.gate.outputs.pr }}" in land
    # The failsafe posts only on the bound PR, never on the caller's number.
    surface = text[text.index("- name: Surface gate failure"):text.index("- name: Stage - Agent")]
    assert "PR: ${{ steps.bind.outputs.pr }}" in surface and "inputs.pr_number" not in surface
    assert "steps.bind.outcome == 'failure'" in surface


def test_the_fix_job_requires_the_tested_head():
    text = WORKFLOW.read_text()
    base = step_block(text, "base")
    assert "TESTED_SHA: ${{ needs.gate.outputs.head_sha }}" in base
    assert 'if [ "$sha" != "$TESTED_SHA" ]; then' in base and "exit 1" in base
    # the withholding and the refund are covered by the outcome-keyed test above


def test_the_example_stub_forwards_the_events_own_fields():
    # The stub's part of the contract: the three inputs are the event's own
    # fields, so the reusable's bind step compares like with like. The
    # comment says what pull_requests[0] is and is not.
    text = EXAMPLE.read_text()
    assert "head_branch: ${{ github.event.workflow_run.head_branch }}" in text
    assert "pr_number: ${{ github.event.workflow_run.pull_requests[0].number }}" in text
    assert "ci_run_id: ${{ github.event.workflow_run.id }}" in text
    assert "`pull_requests[0].number` is an ASSOCIATION, not the triggering PR" in text
