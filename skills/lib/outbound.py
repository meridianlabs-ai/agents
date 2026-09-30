#!/usr/bin/env python3
"""Guards for text a local skill republishes under the maintainer's login.

Rule (d) of skills/THREAT_MODEL.md: text that came from an input the skill
does not trust, published by the skill as the maintainer, is guarded before
it is posted. Two guards live here; the Markdown reference check, which
needs GitHub's renderer, is `render_markdown` / `rendered_refs` in
common.sh.

    outbound.py defang < text > text
    outbound.py defang-review < review.json > review.json
    outbound.py commit-refs --repo OWNER/REPO [--allow N]... < commits

`defang` backticks the agents' trigger phrases and splits the loops'
`<!-- … -->` markers, case-insensitively: the rewrite the land composite
applies to every body the machine account republishes, plus `@i-am-marvin`,
the dev stub's other phrase. Keep the list in step with land's.
`defang-review` applies it to a pull request review's `body` and to each
`comments[].body`, and leaves every other field as it is.

`commit-refs` reads one JSON array per line, `[total_commits, sha,
message]` (the compare API's `total_commits` repeated on every line, so a
truncated listing is visible), and reports each commit whose message
references an issue or PR in REPO's tracker other than the allowed numbers.
Commit messages are plain text, not Markdown: GitHub links a reference in
one wherever it appears, and a squash merge copies the messages onto the
base branch, where a closing keyword before one closes that issue. So the
scan is textual and errs towards reporting. Exit 0 when no commit carries
one, 1 when some do (one line per commit on stdout), 2 when the listing is
malformed or incomplete (fail closed).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable


def defang(text: str) -> str:
    """Backtick the trigger phrases and split the loop markers."""
    text = re.sub(r"@(review|claude|auto|i-am-marvin)", r"`\1`", text, flags=re.I)
    text = re.sub(r"claude-review-(summary|verdict|comment|nudge)", r"claude-review \1", text, flags=re.I)
    text = re.sub(r"auto-(handoff|converged|review-rounds|review-head|fix-attempts)", r"auto \1", text, flags=re.I)
    return text


def defang_review(review: dict) -> dict:
    """`review` with `defang` applied to its body and each inline comment's body."""
    out = dict(review)
    if isinstance(out.get("body"), str):
        out["body"] = defang(out["body"])
    if isinstance(out.get("comments"), list):
        out["comments"] = [
            {**c, "body": defang(c["body"])} if isinstance(c, dict) and isinstance(c.get("body"), str) else c
            for c in out["comments"]
        ]
    return out


def text_refs(text: str, repo: str, allow: Iterable[int] = ()) -> list[int]:
    """Issue and PR numbers of `repo` that plain `text` references, other than `allow`.

    The forms GitHub resolves against the repository the text lands in: a
    bare `#N` (not after a word character, `/` or `&`), `GH-N`, the
    qualified `OWNER/REPO#N` and an issue or PR URL. A reference qualified
    to another repository is not one of `repo`'s.
    """
    name = re.escape(repo)
    patterns = (
        r"(?<![\w/&])#(\d+)\b",
        r"(?<![\w/])GH-(\d+)\b",
        rf"(?<![\w.-]){name}#(\d+)\b",
        rf"https?://github\.com/{name}/(?:issues|pull)/(\d+)",
    )
    allowed = set(allow)
    found = {int(m.group(1)) for p in patterns for m in re.finditer(p, text, flags=re.I)}
    return sorted(found - allowed)


def commit_refs(lines: Iterable[str], repo: str, allow: Iterable[int] = ()) -> tuple[list[str], str | None]:
    """(one report line per offending commit, error) for a `[total, sha, message]` listing."""
    allowed = list(allow)
    seen = 0
    total: int | None = None
    offenders: list[str] = []
    for raw in lines:
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            return offenders, f"not JSON: {raw[:80]!r}"
        if not (
            isinstance(entry, list)
            and len(entry) == 3
            and isinstance(entry[0], int)
            and isinstance(entry[1], str)
            and isinstance(entry[2], str)
        ):
            return offenders, f"unexpected entry: {raw[:80]!r}"
        if total is None:
            total = entry[0]
        elif entry[0] != total:
            return offenders, "total_commits changed between pages"
        seen += 1
        sha, message = entry[1], entry[2]
        refs = text_refs(message, repo, allowed)
        if refs:
            subject = message.splitlines()[0] if message else ""
            offenders.append(f"{sha} ({subject[:72]}): " + " ".join(f"#{n}" for n in refs))
    if total is None:
        total = 0
    if seen != total:
        return offenders, f"listed {seen} of {total} commits"
    return offenders, None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="outbound.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("defang")
    sub.add_parser("defang-review")
    refs = sub.add_parser("commit-refs")
    refs.add_argument("--repo", required=True)
    refs.add_argument("--allow", type=int, action="append", default=[])
    args = parser.parse_args(argv)
    if args.cmd == "defang":
        sys.stdout.write(defang(sys.stdin.read()))
        return 0
    if args.cmd == "defang-review":
        try:
            review = json.load(sys.stdin)
        except ValueError as exc:
            print(f"outbound.py defang-review: not JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(review, dict):
            print("outbound.py defang-review: not a JSON object", file=sys.stderr)
            return 2
        json.dump(defang_review(review), sys.stdout)
        return 0
    offenders, error = commit_refs(sys.stdin, args.repo, args.allow)
    for line in offenders:
        print(line)
    if error:
        print(f"outbound.py commit-refs: {error}", file=sys.stderr)
        return 2
    return 1 if offenders else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
