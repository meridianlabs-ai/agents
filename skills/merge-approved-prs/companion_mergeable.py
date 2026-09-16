#!/usr/bin/env python3
"""Verify a ts-mono companion PR's own review state before the merge queue
merges it, instead of inferring it from the board stage.

The merge-approved-prs skill merges an inspect_ai PR's ts-mono companion with
`gh pr merge --squash` on the strength of the anchor issue sitting at
Stage=Merge. That stage is set by the hourly Atlas sync from the companion
gate it computes, which the queue then trusted without looking at the
companion itself (Claude Security finding 4121986, fix criterion 2). This
script looks: a companion may merge when a trusted reviewer approved its
current head, or when it is regenerate-only.

    companion_mergeable.py https://github.com/meridianlabs-ai/ts-mono/pull/N
    companion_mergeable.py meridianlabs-ai/ts-mono N

Read-only: `gh api` GETs on the PR, its reviews, its commits and its files
(the lists `--paginate`d), the PR again so a head that moved while the lists
were read fails instead of passing, then one cached collaborator-permission
lookup per login considered. Exit 0 and one stdout line

    approved <sha> by <login> at <time>
    regenerate-only <sha>: <files> by <author>

when the check passes; exit 1 and one stdout line with the reason when it
does not, naming why both rules failed:

    <approval_at_head reason>; not regenerate-only: <reason>
    head moved during the check: was <sha>, now <sha2>
    PR is <state>, not open

and exit 2 with the error on stderr when `gh` fails or the input is
malformed. Everything fails closed: what cannot be verified is a skip, and
the skip is the whole queue item, never an approval by the skill.

The rules. **Approved at head** is approval_at_head.py's rule applied to the
companion: an APPROVED review whose `commit_id` is the current head, whose
reviewer's latest verdict is still APPROVED, by a reviewer with write access
on the companion's repository. **Regenerate-only** is the precedent the skill
cites for merging without a human review (ts-mono #427, #439): the PR is
open against the repository's default branch, its head branch lives in that
repository (not a fork), its author is trusted (TRUSTED_LOGINS or write
access), and its diff modifies nothing but GENERATED_FILES — exactly what the
skill's own regeneration sequence writes: `pnpm --filter
@tsmono/inspect-common types:generate` is `node scripts/generate-types.js`,
whose single `writeFileSync` target is `packages/inspect-common/src/types/generated.ts`
(verified against ts-mono main, 2026-09-15). A rename, an addition, a
deletion, or any other path in the diff — the barrel re-exports in
`src/types/index.ts` included; they are hand-written — needs the approval
rule instead. The set is explicit below so that a change to the generator is
a change here, reviewed.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import approval_at_head as aah
from approval_at_head import GhError, Verdict, gh_api, parse_target

# What `types:generate` writes, relative to the ts-mono root. Nothing else in
# a companion's diff is generated; everything else needs a review.
GENERATED_FILES: frozenset[str] = frozenset({"packages/inspect-common/src/types/generated.ts"})

USAGE = "usage: companion_mergeable.py <https://github.com/OWNER/REPO/pull/N | OWNER/REPO N>"


def pr_login(pr: dict[str, Any]) -> str | None:
    user = pr.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    return login if isinstance(login, str) and login else None


def regenerate_only(
    pr: dict[str, Any], files: list[dict[str, Any]], is_trusted: Callable[[str], bool], repo: str
) -> Verdict:
    """The regenerate-only rule: a trusted author's same-repo PR modifying only GENERATED_FILES."""
    head = pr["head"]["sha"]
    base_ref = pr["base"]["ref"]
    default_branch = (pr["base"].get("repo") or {}).get("default_branch")
    if base_ref != default_branch:
        return Verdict(False, f"base is {base_ref}, not {default_branch}")
    head_repo = (pr["head"].get("repo") or {}).get("full_name")
    if head_repo != repo:
        return Verdict(False, f"head branch lives in {head_repo}, not {repo}")
    author = pr_login(pr)
    if author is None or not is_trusted(author):
        return Verdict(False, f"author {author} is not trusted (no write access on {repo})")
    if not files:
        return Verdict(False, "empty diff")
    touched: list[str] = []
    for entry in files:
        name = entry.get("filename")
        previous = entry.get("previous_filename")
        status = entry.get("status")
        if not isinstance(name, str) or not name:
            return Verdict(False, "a diff entry has no filename")
        if name not in GENERATED_FILES:
            return Verdict(False, f"diff touches {name}, outside the generated set")
        if status != "modified" or previous:
            return Verdict(False, f"{name} is {status}{f' (from {previous})' if previous else ''}, not modified")
        touched.append(name)
    return Verdict(True, f"regenerate-only {head}: {', '.join(sorted(set(touched)))} by {author}")


def check(
    pr: dict[str, Any],
    reviews: list[dict[str, Any]],
    commits: list[dict[str, Any]],
    files: list[dict[str, Any]],
    is_trusted: Callable[[str], bool],
    repo: str,
) -> Verdict:
    """Approved at head, else regenerate-only, else the two reasons."""
    state = pr.get("state")
    if state != "open":
        return Verdict(False, f"PR is {state}, not open")
    approved = aah.check(pr, reviews, commits, is_trusted, repo)
    if approved.ok:
        return approved
    regenerated = regenerate_only(pr, files, is_trusted, repo)
    if regenerated.ok:
        return regenerated
    return Verdict(False, f"{approved.message}; not regenerate-only: {regenerated.message}")


def main(argv: list[str]) -> int:
    try:
        repo, number = parse_target(argv)
    except ValueError:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        pr = gh_api(f"repos/{repo}/pulls/{number}")
        reviews = gh_api(f"repos/{repo}/pulls/{number}/reviews", paginate=True)
        commits = gh_api(f"repos/{repo}/pulls/{number}/commits", paginate=True)
        files = gh_api(f"repos/{repo}/pulls/{number}/files", paginate=True)
        # Read the head again: the lists above took time, and a head that moved
        # meanwhile must fail the check rather than be reported as mergeable.
        again = gh_api(f"repos/{repo}/pulls/{number}")
        shapes = (
            isinstance(pr, dict),
            isinstance(again, dict),
            isinstance(reviews, list),
            isinstance(commits, list),
            isinstance(files, list),
        )
        if not all(shapes):
            raise GhError("unexpected response shape from gh api")
        if again["head"]["sha"] != pr["head"]["sha"]:
            print(f"head moved during the check: was {pr['head']['sha']}, now {again['head']['sha']}")
            return 1
        verdict = check(pr, reviews, commits, files, aah.make_trust_check(repo), repo)
    except (GhError, KeyError, TypeError, ValueError) as exc:
        print(f"companion_mergeable: {exc}", file=sys.stderr)
        return 2
    print(verdict.message)
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
