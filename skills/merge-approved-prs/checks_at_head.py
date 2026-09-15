#!/usr/bin/env python3
"""Require upstream CI to have passed on the commit the merge queue is about to
check out, so the queue never has to run that tree itself.

The merge-approved-prs skill used to sanity-check a merged External PR by
running `ruff`, `mypy` and `pytest` (and, for viewer-schema PRs, `schema.py`)
from the checked-out tree, inside the maintainer's credentialed session. A
contributor's tree is untrusted code; the CI external-review path runs it
only in a sandbox (Claude Security finding 4122327, fix criterion 2). This
script is the queue's substitute for those local runs: it answers whether
upstream's own CI has already run the approved commit and passed.

    checks_at_head.py https://github.com/OWNER/REPO/pull/N [--sha SHA]
    checks_at_head.py OWNER/REPO N [--sha SHA]

Read-only: `gh api` GETs on the PR (for its head SHA and base branch), the
base branch's rules (for the required status checks), the check runs on the
SHA and its commit statuses. Exit 0 and one stdout line

    checks passed at <sha>: N check runs, M statuses; required (K): <names>

when every check run on the SHA has completed with a passing conclusion,
every commit status is `success`, and every status check the base branch's
ruleset requires has reported. Exit 1 and one stdout line with the reason
when not:

    checks at <sha> not green: [missing required: ...] [pending: ...] [failed: ...]
    head is <sha>, not the approved <sha2>
    no required status checks on <repo> <branch>: nothing to defer to

and exit 2 with the error on stderr when `gh` fails or the input is
malformed. Everything fails closed: what cannot be verified is a skip.

The rule. `--sha` is the approved commit from approval_at_head.py; the PR's
head must still be that commit (without `--sha` the head is used). The
required checks come from `repos/{repo}/rules/branches/{base}` — the
`required_status_checks` rule, each context matched by name and, when the
rule names one, by the reporting app's id (GitHub's own matching). A
required check that has not reported is a failure: on GitHub a workflow that
never ran leaves the check pending and the PR blocked, and here the fork-PR
workflow-approval gate is the usual cause. A conclusion of `success`,
`skipped` or `neutral` passes, which is how GitHub evaluates a required
check; anything else (`failure`, `cancelled`, `timed_out`,
`action_required`, `stale`, `startup_failure`) or a run still queued or in
progress fails. Every OTHER check run on the SHA is held to the same
completed-and-passing standard, so a red `ruff` or `check-schema-and-types`
skips the item even though upstream does not require them. The check runs
are read with `filter=latest` (one per name and app, the run GitHub itself
consults) and paginated explicitly by `total_count`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from approval_at_head import GhError, Verdict, gh_api, parse_target

# Conclusions GitHub counts as passing for a required status check.
PASSING = frozenset({"success", "neutral", "skipped"})
PER_PAGE = 100
MAX_PAGES = 10  # 1000 check runs on one commit is beyond anything upstream produces

USAGE = "usage: checks_at_head.py <https://github.com/OWNER/REPO/pull/N | OWNER/REPO N> [--sha SHA]"


def required_checks(rules: list[dict[str, Any]]) -> list[tuple[str, int | None]]:
    """(context, integration_id) for every required status check in the branch's rules."""
    out: list[tuple[str, int | None]] = []
    for rule in rules:
        if rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters") or {}
        for entry in params.get("required_status_checks") or []:
            context = entry.get("context")
            if isinstance(context, str) and context:
                integration = entry.get("integration_id")
                out.append((context, integration if isinstance(integration, int) else None))
    return out


def run_app_id(run: dict[str, Any]) -> int | None:
    app = run.get("app")
    app_id = app.get("id") if isinstance(app, dict) else None
    return app_id if isinstance(app_id, int) else None


def latest_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One run per (name, app): the newest by id. `filter=latest` already does this; belt and braces."""
    latest: dict[tuple[str, int | None], dict[str, Any]] = {}
    for run in runs:
        key = (str(run.get("name")), run_app_id(run))
        if key not in latest or (run.get("id") or 0) > (latest[key].get("id") or 0):
            latest[key] = run
    return list(latest.values())


def describe(run: dict[str, Any]) -> str:
    status = run.get("status")
    return f"{run.get('name')} ({run.get('conclusion') if status == 'completed' else status})"


def check(
    pr: dict[str, Any],
    rules: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    statuses: list[dict[str, Any]],
    repo: str,
    expected: str | None,
) -> Verdict:
    """Decide whether CI on the PR's head (which must be `expected`, if given) is complete and green."""
    head = pr["head"]["sha"]
    base = pr["base"]["ref"]
    if not isinstance(head, str) or not head or not isinstance(base, str) or not base:
        raise GhError("PR has no head sha or base ref")
    if expected is not None and head != expected:
        return Verdict(False, f"head is {head}, not the approved {expected}")

    required = required_checks(rules)
    if not required:
        return Verdict(False, f"no required status checks on {repo} {base}: nothing to defer to")

    runs = latest_runs(runs)
    missing: list[str] = []
    pending: list[str] = []
    failed: list[str] = []
    for context, integration in required:
        if not any(r.get("name") == context and (integration is None or run_app_id(r) == integration) for r in runs):
            missing.append(context)
    for run in runs:
        if run.get("status") != "completed":
            pending.append(describe(run))
        elif run.get("conclusion") not in PASSING:
            failed.append(describe(run))
    for status in statuses:
        if status.get("state") != "success":
            failed.append(f"status {status.get('context')} ({status.get('state')})")

    if missing or pending or failed:
        parts = []
        if missing:
            parts.append("missing required: " + ", ".join(missing))
        if pending:
            parts.append("pending: " + ", ".join(pending))
        if failed:
            parts.append("failed: " + ", ".join(failed))
        return Verdict(False, f"checks at {head} not green: " + "; ".join(parts))

    names = ", ".join(context for context, _ in required)
    return Verdict(
        True,
        f"checks passed at {head}: {len(runs)} check runs, {len(statuses)} statuses; required ({len(required)}): {names}",
    )


def fetch_check_runs(repo: str, sha: str) -> list[dict[str, Any]]:
    """Every check run on `sha`, latest per name and app, paged explicitly by `total_count`."""
    runs: list[dict[str, Any]] = []
    total: int | None = None
    for page in range(1, MAX_PAGES + 1):
        body = gh_api(f"repos/{repo}/commits/{sha}/check-runs?filter=latest&per_page={PER_PAGE}&page={page}")
        if not isinstance(body, dict) or not isinstance(body.get("check_runs"), list):
            raise GhError("unexpected response shape from check-runs")
        total = body.get("total_count") if isinstance(body.get("total_count"), int) else None
        runs.extend(body["check_runs"])
        if total is None or len(runs) >= total or not body["check_runs"]:
            break
    if total is not None and len(runs) < total:
        raise GhError(f"read {len(runs)} of {total} check runs on {sha}")
    return runs


def fetch_statuses(repo: str, sha: str) -> list[dict[str, Any]]:
    """The commit statuses on `sha` (latest per context); more than one page is an error."""
    body = gh_api(f"repos/{repo}/commits/{sha}/status?per_page={PER_PAGE}")
    if not isinstance(body, dict) or not isinstance(body.get("statuses"), list):
        raise GhError("unexpected response shape from status")
    statuses: list[dict[str, Any]] = body["statuses"]
    total = body.get("total_count")
    if isinstance(total, int) and total > len(statuses):
        raise GhError(f"read {len(statuses)} of {total} statuses on {sha}")
    return statuses


def parse_args(argv: list[str]) -> tuple[str, int, str | None]:
    """`<pr-url>` or `<owner/repo> <number>`, optionally `--sha SHA` → (owner/repo, number, sha)."""
    sha: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--sha":
            if i + 1 >= len(argv) or sha is not None:
                raise ValueError(USAGE)
            sha = argv[i + 1]
            i += 2
        elif argv[i].startswith("--sha="):
            if sha is not None:
                raise ValueError(USAGE)
            sha = argv[i][len("--sha=") :]
            i += 1
        else:
            rest.append(argv[i])
            i += 1
    if sha is not None and not (len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)):
        raise ValueError(USAGE)
    try:
        repo, number = parse_target(rest)
    except ValueError:
        raise ValueError(USAGE) from None
    return repo, number, sha


def main(argv: list[str]) -> int:
    try:
        repo, number, expected = parse_args(argv)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        pr = gh_api(f"repos/{repo}/pulls/{number}")
        if not isinstance(pr, dict):
            raise GhError("unexpected response shape from gh api")
        head, base = pr["head"]["sha"], pr["base"]["ref"]
        if expected is not None and head != expected:
            # Say so before reading anything about a commit nobody approved.
            print(f"head is {head}, not the approved {expected}")
            return 1
        rules = gh_api(f"repos/{repo}/rules/branches/{base}")
        if not isinstance(rules, list):
            raise GhError("unexpected response shape from rules")
        runs = fetch_check_runs(repo, head)
        statuses = fetch_statuses(repo, head)
        verdict = check(pr, rules, runs, statuses, repo, expected)
    except (GhError, KeyError, TypeError, ValueError) as exc:
        print(f"checks_at_head: {exc}", file=sys.stderr)
        return 2
    print(verdict.message)
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
