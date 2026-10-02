"""Tests for skills/merge-approved-prs/SKILL.md section 1 — finding the queue.

The section's block is lifted from SKILL.md and run against a stub `gh` on
PATH that serves canned GraphQL pages (in the shape the live Atlas query
returned, 2026-10-02) and applies the block's `--jq` filter to each, as
`gh api graphql --paginate --jq` does. That pins the TSV the later sections
read (number, repository, title, linked PRs) for Merge-stage items on every
page, and that the block pages the board with a narrow GraphQL query instead
of exporting it with `gh project item-list` (decision: Ransom, 2026-10-02).
Run with `python3 -m pytest` from the repo root.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "merge-approved-prs" / "SKILL.md"

FAKE_GH_PY = r"""
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
if args[:2] != ["api", "graphql"] or "--paginate" not in args:
    sys.stderr.write("stub gh: only `gh api graphql --paginate` is served\n")
    sys.exit(1)
jq = args[args.index("--jq") + 1]
pages = json.load(open(os.environ["FAKE_GH_PAGES"]))
fail = int(os.environ.get("FAKE_GH_FAIL_PAGE", "0"))
for i, page in enumerate(pages, 1):
    if i == fail:
        sys.stderr.write("gh: Something went wrong (HTTP 502)\n")
        sys.exit(1)
    out = subprocess.run(["jq", "-r", jq], input=json.dumps(page), text=True, capture_output=True)
    if out.returncode:
        sys.stderr.write(out.stderr)
        sys.exit(1)
    sys.stdout.write(out.stdout)
"""


def queue_block():
    """The fenced block of SKILL.md's "1. Find the queue"."""
    text = SKILL.read_text()
    start = text.index("## 1. Find the queue")
    opened = text.index("```bash\n", start) + len("```bash\n")
    return text[opened : text.index("```", opened)]


def item(stage, content, prs=None):
    return {
        "stage": {"name": stage} if stage else None,
        "prs": {"pullRequests": {"nodes": [{"url": u} for u in prs]}} if prs is not None else None,
        "content": content,
    }


def issue(number, title, repo="meridianlabs-ai/inspect_ai"):
    return {"number": number, "title": title, "repository": {"url": f"https://github.com/{repo}"}}


def page(nodes, cursor):
    return {
        "data": {
            "organization": {
                "projectV2": {
                    "items": {"pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}, "nodes": nodes}
                }
            }
        }
    }


UP = "https://github.com/UKGovernmentBEIS/inspect_ai/pull"
FORK = "https://github.com/meridianlabs-ai/inspect_ai/pull"
PAGES = [
    page(
        [
            item("Merge", issue(73, "recover: mark incomplete samples"), [f"{FORK}/76", f"{UP}/5165"]),
            item("Review", issue(388, "Review upstream #4944"), [f"{UP}/4944"]),
            item(None, issue(443, "Review 5179")),
            item("Sign-off", issue(99, "release-please-vscode", "meridianlabs-ai/actions"), [f"{FORK}/102"]),
        ],
        "Y3Vyc29yOjEwMA==",
    ),
    page(
        [
            item("Merge", issue(512, "External: fix the bridge"), []),
            item("Merge", issue(513, "Promotion, no chip yet")),
            item(None, {"title": "a draft"}),
        ],
        None,
    ),
]


def run_block(tmp_path, block, fail_page=0):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "fake_gh.py").write_text(FAKE_GH_PY)
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fake_gh.py"}" "$@"\n')
    gh.chmod(0o755)
    pages = tmp_path / "pages.json"
    pages.write_text(json.dumps(PAGES))
    log = tmp_path / "gh.log"
    log.write_text("")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_PAGES": str(pages),
        "FAKE_GH_LOG": str(log),
        "FAKE_GH_FAIL_PAGE": str(fail_page),
    }
    r = subprocess.run(["bash", "-c", block], text=True, capture_output=True, env=env, check=False)
    return r, [json.loads(line) for line in log.read_text().splitlines()]


def test_the_queue_block_lists_merge_items_from_every_page(tmp_path):
    r, calls = run_block(tmp_path, queue_block())
    assert r.returncode == 0, r.stderr
    repo = "https://github.com/meridianlabs-ai/inspect_ai"
    assert r.stdout.splitlines() == [
        f"73\t{repo}\trecover: mark incomplete samples\t{FORK}/76,{UP}/5165",
        f"512\t{repo}\tExternal: fix the bridge\t",
        f"513\t{repo}\tPromotion, no chip yet\t",
    ]
    assert len(calls) == 1


def test_a_failed_page_fails_the_block(tmp_path):
    r, _ = run_block(tmp_path, queue_block(), fail_page=2)
    assert r.returncode != 0
    assert "HTTP 502" in r.stderr


def test_the_query_pages_the_board_and_reads_only_the_fields_it_uses(tmp_path):
    block = queue_block()
    assert "item-list" not in block
    _, calls = run_block(tmp_path, block)
    (args,) = calls
    query = next(a.split("=", 1)[1] for a in args if a.startswith("query="))
    assert "--paginate" in args
    assert "query($endCursor: String)" in query
    assert "items(first: 100, after: $endCursor)" in query
    assert "pageInfo { hasNextPage endCursor }" in query
    assert 'fieldValueByName(name: "Stage")' in query
    assert "Status" not in query
    # Only the three values the TSV needs, no full field export.
    assert "fieldValues" not in query
    assert query.count("fieldValueByName") == 2
