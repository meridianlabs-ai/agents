#!/usr/bin/env python3
"""Bind a PR's approval to the commit the merge queue is about to check out.

`reviewDecision` is PR-level: unless the upstream repository dismisses
stale approvals on push (a setting this repo neither knows nor requires),
it survives a contributor's push, so "APPROVED" says nothing about the head
the merge-approved-prs skill would check out, run pytest on and auto-merge
(Claude Security finding 4122327, CWE-367). This script answers the
question that gates that checkout: is the CURRENT head the exact commit a
trusted reviewer approved?

    approval_at_head.py https://github.com/OWNER/REPO/pull/N
    approval_at_head.py OWNER/REPO N

Read-only: four `gh api` GETs on the PR (the PR itself for its head SHA,
its reviews, its commits, then the PR again so a head that moved while the
lists were being read fails the check instead of being reported as
approved) plus one collaborator-permission lookup per approver considered,
cached per login. Exit 0 and one stdout line

    approved <sha> by <login> at <time>

when the check passes; exit 1 and one stdout line with the reason when it
does not:

    approval is for <sha>, head is <sha2>; N commits pushed after <time>
    no approval for head <sha>[: <detail>]
    head moved during the check: was <sha>, now <sha2>

and exit 2 with the error on stderr when `gh` fails or the input is
malformed. Everything fails closed: what cannot be verified is a skip.

The rule. A review counts when its state is APPROVED, its `commit_id` is
the PR's current head (`headRefOid`), its reviewer's most recent verdict is
still APPROVED, and the reviewer is trusted. A verdict is an APPROVED,
CHANGES_REQUESTED or DISMISSED review; COMMENTED reviews carry no verdict
and change nothing, as on GitHub itself, and PENDING (unsubmitted) reviews
are ignored. Trusted means named in TRUSTED_LOGINS or holding write,
maintain or admin on the repository: anyone can submit an APPROVED review
on a public PR, only a collaborator's satisfies branch protection, so only
a collaborator's is believed here — otherwise a contributor could push and
then "approve" the new head from a second account while the maintainer's
stale approval keeps `reviewDecision` at APPROVED.

The commit SHA pins the whole tree and history, so SHA equality with the
head IS the binding: a force-push away and back to the identical SHA is the
same content and passes. Commit dates are diagnostic only — the mismatch
reason counts the PR commits dated after the approval — because committer
timestamps come from the contributor's clock (observed on
UKGovernmentBEIS/inspect_ai#5360: the approved head's committer date was
eight minutes AFTER the approval that named it), so they cannot be a gate
without skipping legitimate PRs.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any, NamedTuple

# Logins believed without a permission lookup — the one place to change when
# the trusted identity changes (handoff section 3). Empty on purpose: the
# approvals the merge queue acts on come from upstream maintainers, whom the
# collaborator-permission lookup identifies; no login is trusted by name.
# The machine account (`i-am-marvin`, and `meridian-marvin[bot]` in Phase 2)
# stays out: it holds only `read` on UKGovernmentBEIS/inspect_ai (checked
# 2026-09-16) and may not supply an upstream approval (decision: Ransom,
# 2026-09-16, agents#110). The companion rule that must trust the machine
# account as a PR AUTHOR has its own set: companion_mergeable.TRUSTED_AUTHORS.
TRUSTED_LOGINS: frozenset[str] = frozenset()

# `permission` collapses maintain→write and triage→read; `role_name` is the
# finer value. Either field naming one of these grants trust.
WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})
VERDICT_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})

PR_URL_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)(?:/[^#?]*)?(?:[#?].*)?$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# GitHub logins are alphanumerics and hyphens; app identities carry `[bot]`.
# Anything else never reaches a request path (the login came from the API,
# but the permission lookup interpolates it into a URL, so it is validated
# rather than trusted).
LOGIN_RE = re.compile(r"^[A-Za-z0-9-]+(\[bot\])?$")

USAGE = "usage: approval_at_head.py <https://github.com/OWNER/REPO/pull/N | OWNER/REPO N>"


class GhError(Exception):
    """`gh api` failed or returned something that is not the expected JSON."""


class Verdict(NamedTuple):
    ok: bool
    message: str


def parse_json_stream(text: str) -> Any:
    """Decode `gh api` output: one JSON value, or `--paginate`'s pages.

    Older gh prints each page as its own array back to back; newer gh joins
    them into one array. Both shapes are handled: consecutive arrays are
    concatenated, a single value is returned as is.
    """
    decoder = json.JSONDecoder()
    values: list[Any] = []
    pos, end = 0, len(text)
    while pos < end:
        while pos < end and text[pos].isspace():
            pos += 1
        if pos >= end:
            break
        value, pos = decoder.raw_decode(text, pos)
        values.append(value)
    if not values:
        raise GhError("empty response")
    if len(values) == 1:
        return values[0]
    if all(isinstance(v, list) for v in values):
        return [item for page in values for item in page]
    raise GhError("unexpected multi-value response")


def gh_api(path: str, *, paginate: bool = False) -> Any:
    """GET `path` through `gh api`. Never passes a method or body: reads only."""
    cmd = ["gh", "api", path]
    if paginate:
        cmd.append("--paginate")
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise GhError(f"gh api {path}: {detail}")
    try:
        return parse_json_stream(proc.stdout)
    except ValueError as exc:
        raise GhError(f"gh api {path}: not JSON: {exc}") from exc


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def review_login(review: dict[str, Any]) -> str | None:
    user = review.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    return login if isinstance(login, str) and login else None


def make_trust_check(repo: str, api: Callable[..., Any] = gh_api) -> Callable[[str], bool]:
    """Return `is_trusted(login)`: TRUSTED_LOGINS, else a cached permission lookup.

    A failed or malformed lookup, or a login that is not shaped like one, is
    untrusted (fail closed).
    """
    trusted_by_name = {login.lower() for login in TRUSTED_LOGINS}
    cache: dict[str, bool] = {}

    def is_trusted(login: str) -> bool:
        key = login.lower()
        if key in cache:
            return cache[key]
        if key in trusted_by_name:
            cache[key] = True
            return True
        ok = False
        if LOGIN_RE.match(login):
            try:
                perm = api(f"repos/{repo}/collaborators/{login}/permission")
            except GhError:
                perm = None
            if isinstance(perm, dict):
                ok = perm.get("permission") in WRITE_PERMISSIONS or perm.get("role_name") in WRITE_PERMISSIONS
        cache[key] = ok
        return ok

    return is_trusted


def commits_after(commits: list[dict[str, Any]], when: str) -> int:
    """PR commits whose committer date is after `when` (diagnostic only)."""
    cutoff = parse_time(when)
    count = 0
    for commit in commits:
        date = ((commit.get("commit") or {}).get("committer") or {}).get("date")
        if isinstance(date, str) and parse_time(date) > cutoff:
            count += 1
    return count


def check(
    pr: dict[str, Any],
    reviews: list[dict[str, Any]],
    commits: list[dict[str, Any]],
    is_trusted: Callable[[str], bool],
    repo: str,
) -> Verdict:
    """Decide whether the PR's current head carries a standing, trusted approval."""
    head = pr["head"]["sha"]
    if not isinstance(head, str) or not head:
        raise GhError("PR has no head sha")

    submitted = sorted(
        (r for r in reviews if isinstance(r.get("submitted_at"), str) and r.get("state") != "PENDING"),
        key=lambda r: (r["submitted_at"], r.get("id") or 0),
    )
    # Each reviewer's standing verdict: their latest APPROVED / CHANGES_REQUESTED /
    # DISMISSED review. COMMENTED never overrides a verdict.
    standing: dict[str, dict[str, Any]] = {}
    for review in submitted:
        login = review_login(review)
        if login and review["state"] in VERDICT_STATES:
            standing[login] = review

    def standing_state(review: dict[str, Any]) -> str:
        login = review_login(review)
        return standing[login]["state"] if login and login in standing else ""

    approvals = [r for r in submitted if r["state"] == "APPROVED" and review_login(r)]
    at_head = [r for r in approvals if r.get("commit_id") == head]
    at_head_standing = [r for r in at_head if standing_state(r) == "APPROVED"]

    # Latest first: the newest approval of this exact head from a reviewer
    # who has not since changed their mind, by someone who may approve.
    untrusted: str | None = None
    for review in reversed(at_head_standing):
        login = review_login(review)
        assert login is not None
        if is_trusted(login):
            return Verdict(True, f"approved {head} by {login} at {review['submitted_at']}")
        untrusted = untrusted or login
    if untrusted:
        return Verdict(
            False, f"no approval for head {head}: approval by {untrusted} does not count (no write access on {repo})"
        )

    superseded = [r for r in at_head if r not in at_head_standing]
    if superseded:
        review = superseded[-1]
        login = review_login(review)
        assert login is not None
        later = standing[login]
        return Verdict(
            False,
            f"no approval for head {head}: {login} approved it at {review['submitted_at']} "
            f"but their latest review is {later['state']} at {later['submitted_at']}",
        )

    elsewhere = [r for r in approvals if r.get("commit_id") != head and standing_state(r) == "APPROVED"]
    if elsewhere:
        review = elsewhere[-1]
        when = review["submitted_at"]
        moved = commits_after(commits, when)
        if moved:
            tail = f"{moved} commit{'s' if moved != 1 else ''} pushed after {when}"
        else:
            tail = f"head moved after {when} (no PR commit is dated later: rewritten history)"
        return Verdict(False, f"approval is for {review.get('commit_id')}, head is {head}; {tail}")

    if standing:
        latest = max(standing.values(), key=lambda r: (r["submitted_at"], r.get("id") or 0))
        login = review_login(latest)
        return Verdict(
            False,
            f"no approval for head {head}: latest verdict is {latest['state']} by {login} at {latest['submitted_at']}",
        )
    return Verdict(False, f"no approval for head {head}")


def parse_target(argv: list[str]) -> tuple[str, int]:
    """`<pr-url>` or `<owner/repo> <number>` → (owner/repo, number)."""
    if len(argv) == 1:
        match = PR_URL_RE.match(argv[0])
        if match:
            return f"{match.group(1)}/{match.group(2)}", int(match.group(3))
    elif len(argv) == 2 and REPO_RE.match(argv[0]) and argv[1].isdigit():
        return argv[0], int(argv[1])
    raise ValueError(USAGE)


def main(argv: list[str]) -> int:
    try:
        repo, number = parse_target(argv)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        pr = gh_api(f"repos/{repo}/pulls/{number}")
        reviews = gh_api(f"repos/{repo}/pulls/{number}/reviews", paginate=True)
        commits = gh_api(f"repos/{repo}/pulls/{number}/commits", paginate=True)
        # Read the head again: the lists above took time, and a head that moved
        # meanwhile must fail the check rather than be reported as approved.
        again = gh_api(f"repos/{repo}/pulls/{number}")
        shapes = (isinstance(pr, dict), isinstance(again, dict), isinstance(reviews, list), isinstance(commits, list))
        if not all(shapes):
            raise GhError("unexpected response shape from gh api")
        if again["head"]["sha"] != pr["head"]["sha"]:
            print(f"head moved during the check: was {pr['head']['sha']}, now {again['head']['sha']}")
            return 1
        verdict = check(pr, reviews, commits, make_trust_check(repo), repo)
    except (GhError, KeyError, TypeError, ValueError) as exc:
        print(f"approval_at_head: {exc}", file=sys.stderr)
        return 2
    print(verdict.message)
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
