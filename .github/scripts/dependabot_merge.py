#!/usr/bin/env python3
"""Hourly Dependabot merge job (workflow: .github/workflows/dependabot-merge.yml).

For each repository the `meridian-dependabot-merger` GitHub App is installed
on (or each `--repo`), the job reads the default branch's rules and skips the
repository when they require no status checks. Otherwise it makes sure the
`no-auto-merge` label exists, and looks at every open PR that
`dependabot[bot]` opened against the default branch. A PR is left alone when:

  - it carries the `no-auto-merge` label;
  - any commit on it is not a verified Dependabot commit, or its head moved
    while the job read it;
  - auto-merge is already on, or the App already approved its current head;
  - the policy file (.github/dependabot-merge-policy.yml) does not allow
    every package it updates: the dependency type comes from the branch name
    (dependabot/<type>/...), the bump type from the `updated-dependencies`
    block of the commit messages, and a package in `always_ask` never
    merges.

Any other PR is approved at its head commit, then merged (squash, pinned to
that commit) when every required check has already passed, or given
auto-merge (pinned to that commit) when not. The job never updates a branch
and does nothing about a failed check.

Without `--live` (the default) the job writes nothing: it logs every
decision and every write it would make. Every write goes through `write()`.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

DEPENDABOT = "dependabot[bot]"
# GitHub signs the commits Dependabot makes; their committer is web-flow.
DEPENDABOT_COMMITTER = "web-flow"
APP_BOT = "meridian-dependabot-merger[bot]"
OPT_OUT_LABEL = "no-auto-merge"
POLICY_PATH = Path(__file__).resolve().parents[1] / "dependabot-merge-policy.yml"
BUMPS = {
    "version-update:semver-patch": "patch",
    "version-update:semver-minor": "minor",
    "version-update:semver-major": "major",
}
# Check-run conclusions GitHub counts as passing for a required check.
PASSING = frozenset({"success", "neutral", "skipped"})
BRANCH_RE = re.compile(r"dependabot/([^/]+)/")
METADATA_RE = re.compile(r"^---\n(updated-dependencies:\n.*?)^\.\.\.$", re.M | re.S)

log_lines: list[str] = []


def log(line: str) -> None:
    print(line, flush=True)
    log_lines.append(line)


def gh(*args: str) -> str:
    res = subprocess.run(["gh", *args], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:4])}: {res.stderr.strip()[:400]}")
    return res.stdout


def gh_json(*args: str):
    return json.loads(gh(*args))


def gh_pages(path: str) -> list:
    """Every page of a REST list endpoint, as one list of pages."""
    return gh_json("api", "--paginate", "--slurp", path)


def write(live: bool, description: str, *args: str) -> None:
    """The one place the job writes to GitHub. Dry run: log it, write nothing."""
    if live:
        gh(*args)
    log(f"  {description}" if live else f"  would {description}")


@dataclass(frozen=True)
class Policy:
    allow: dict[str, frozenset[str]]
    always_ask: frozenset[str]


def load_policy(text: str) -> Policy:
    data = yaml.safe_load(text)
    if set(data) != {"allow", "always_ask"}:
        raise ValueError(f"policy keys must be allow and always_ask, got {sorted(data)}")
    allow = {k: frozenset(v) for k, v in (data["allow"] or {}).items()}
    unknown = set().union(*allow.values()) - set(BUMPS.values())
    if unknown:
        raise ValueError(f"policy: unknown bump types {sorted(unknown)}")
    return Policy(allow=allow, always_ask=frozenset(data["always_ask"] or []))


def updated_dependencies(message: str) -> list[dict] | None:
    """The `updated-dependencies` entries of a Dependabot commit message, or
    None when the message has no such block."""
    m = METADATA_RE.search(message)
    return yaml.safe_load(m.group(1))["updated-dependencies"] if m else None


def is_dependabot_commit(commit: dict) -> bool:
    return (
        (commit.get("author") or {}).get("login") == DEPENDABOT
        and (commit.get("committer") or {}).get("login") == DEPENDABOT_COMMITTER
        and commit["commit"]["verification"]["verified"]
    )


def dependency_reason(dep: dict, dep_type: str, policy: Policy) -> str | None:
    name = dep["dependency-name"]
    bump = BUMPS.get(dep.get("update-type", ""))
    if name in policy.always_ask:
        return f"{name} is in always_ask"
    if bump not in policy.allow.get(dep_type, frozenset()):
        return f"{name}: {bump or 'unknown'} bump not allowed for {dep_type}"
    return None


def skip_reason(pr: dict, commits: list[dict], reviews: list[dict], policy: Policy) -> str | None:
    """Why the job leaves this PR alone, or None when it acts on it."""
    sha = pr["head"]["sha"]
    if OPT_OUT_LABEL in (label["name"] for label in pr["labels"]):
        return f"labeled {OPT_OUT_LABEL}"
    others = [c["sha"][:10] for c in commits if not is_dependabot_commit(c)]
    if others:
        return f"commits not from Dependabot: {', '.join(others)}"
    if not commits or commits[-1]["sha"] != sha:
        return "head moved while the job read it"
    if pr["auto_merge"]:
        return "auto-merge already on"
    if any(
        r["user"]["login"] == APP_BOT and r["state"] == "APPROVED" and r["commit_id"] == sha
        for r in reviews
    ):
        return "already approved at this head"
    branch = BRANCH_RE.match(pr["head"]["ref"])
    if not branch:
        return f"no dependency type in branch {pr['head']['ref']}"
    blocks = [updated_dependencies(c["commit"]["message"]) for c in commits]
    if None in blocks:
        return "a commit has no updated-dependencies block"
    reasons = [
        reason
        for block in blocks
        for dep in block
        if (reason := dependency_reason(dep, branch.group(1), policy))
    ]
    return "; ".join(reasons) or None


def required_checks(rules: list[dict]) -> list[tuple[str, int | None]]:
    """(context, integration_id) of every required status check in a
    branch's rules, from every ruleset that applies."""
    return [
        (check["context"], check.get("integration_id"))
        for rule in rules
        if rule["type"] == "required_status_checks"
        for check in rule["parameters"]["required_status_checks"]
    ]


def check_passed(context: str, app_id: int | None, runs: list[dict], statuses: list[dict]) -> bool:
    matching = [
        r for r in runs if r["name"] == context and (app_id is None or r["app"]["id"] == app_id)
    ]
    if matching:
        return all(r["status"] == "completed" and r["conclusion"] in PASSING for r in matching)
    return app_id is None and any(
        s["context"] == context and s["state"] == "success" for s in statuses
    )


def checks_passed(required, runs: list[dict], statuses: list[dict]) -> bool:
    return all(check_passed(context, app_id, runs, statuses) for context, app_id in required)


def ensure_label(repo: str, live: bool) -> None:
    names = {label["name"] for page in gh_pages(f"repos/{repo}/labels?per_page=100") for label in page}
    if OPT_OUT_LABEL not in names:
        write(
            live,
            f"create label {OPT_OUT_LABEL} in {repo}",
            "api", "-X", "POST", f"repos/{repo}/labels",
            "-f", f"name={OPT_OUT_LABEL}",
            "-f", "color=d93f0b",
            "-f", "description=The Dependabot merge job leaves this PR for a person",
        )


def handle_pr(repo: str, pr: dict, required, policy: Policy, live: bool) -> None:
    n, sha = pr["number"], pr["head"]["sha"]
    commits = [c for page in gh_pages(f"repos/{repo}/pulls/{n}/commits?per_page=100") for c in page]
    reviews = [r for page in gh_pages(f"repos/{repo}/pulls/{n}/reviews?per_page=100") for r in page]
    reason = skip_reason(pr, commits, reviews, policy)
    if reason:
        log(f"{repo}#{n} skip: {reason}")
        return
    runs = [
        r
        for page in gh_pages(f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
        for r in page["check_runs"]
    ]
    statuses = gh_json("api", f"repos/{repo}/commits/{sha}/status")["statuses"]
    passed = checks_passed(required, runs, statuses)
    log(f"{repo}#{n} act: required checks {'passed' if passed else 'not all passed yet'}")
    write(
        live,
        f"approve {repo}#{n} at {sha[:10]}",
        "api", "-X", "POST", f"repos/{repo}/pulls/{n}/reviews",
        "-f", f"commit_id={sha}", "-f", "event=APPROVE",
    )
    if passed:
        write(
            live,
            f"squash-merge {repo}#{n} at {sha[:10]}",
            "api", "-X", "PUT", f"repos/{repo}/pulls/{n}/merge",
            "-f", f"sha={sha}", "-f", "merge_method=squash",
        )
    else:
        write(
            live,
            f"enable auto-merge (squash) on {repo}#{n} at {sha[:10]}",
            "api", "graphql",
            "-f", "query=mutation($pr:ID!,$sha:GitObjectID!){enablePullRequestAutoMerge("
            "input:{pullRequestId:$pr,mergeMethod:SQUASH,expectedHeadOid:$sha}){clientMutationId}}",
            "-f", f"pr={pr['node_id']}", "-f", f"sha={sha}",
        )


def handle_repo(repo: str, policy: Policy, live: bool) -> bool:
    """Act on one repository's Dependabot PRs; False when any of them failed."""
    branch = gh_json("api", f"repos/{repo}")["default_branch"]
    required = required_checks(gh_json("api", f"repos/{repo}/rules/branches/{branch}"))
    if not required:
        log(f"{repo} skip: no required status checks on {branch}")
        return True
    ensure_label(repo, live)
    prs = [
        pr
        for page in gh_pages(f"repos/{repo}/pulls?state=open&base={branch}&per_page=100")
        for pr in page
        if pr["user"]["login"] == DEPENDABOT
    ]
    log(f"{repo}: {len(prs)} open Dependabot PRs, {len(required)} required checks")
    ok = True
    for pr in prs:
        try:
            handle_pr(repo, pr, required, policy, live)
        except Exception as e:  # noqa: BLE001 — one PR's failure must not stop the others
            log(f"::error::{repo}#{pr['number']}: {e}")
            ok = False
    return ok


def installation_repos() -> list[str]:
    return [
        r["full_name"]
        for page in gh_pages("installation/repositories?per_page=100")
        for r in page["repositories"]
        if not r["archived"]
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", action="append", help="owner/name; default: the App's repositories")
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    parser.add_argument("--live", action="store_true", help="write; without it, log only")
    args = parser.parse_args(argv)
    policy = load_policy(args.policy.read_text(encoding="utf-8"))
    log(f"mode: {'live' if args.live else 'dry run (no writes)'}")

    def guarded(repo: str) -> bool:
        try:
            return handle_repo(repo, policy, args.live)
        except Exception as e:  # noqa: BLE001 — one repo's failure must not stop the others
            log(f"::error::{repo}: {e}")
            return False

    results = [guarded(repo) for repo in args.repo or installation_repos()]
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Dependabot merge\n\n```\n" + "\n".join(log_lines) + "\n```\n")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
