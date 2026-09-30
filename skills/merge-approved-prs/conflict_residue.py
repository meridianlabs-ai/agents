#!/usr/bin/env python3
"""Find conflict markers an unfinished merge resolution left behind.

Used by external.sh (commit and push) on an External PR's tree. `git diff
--check` is not enough there: it skips any path the contributor's
`.gitattributes` classifies as binary (`-diff`, or a diff driver with
`binary = true`), while the merge still wrote text conflict markers into it.
So this reads the blobs themselves, whatever the attributes say about
diffing:

    conflict_residue.py <parent1> <parent2> <result>

`<result>` is `--cached` (the index) or a commit. For every path whose
content in the result differs from either parent, it counts the marker
lines in the result — `<`, `=`, `>` and `|` runs of the path's
conflict-marker-size (its attribute, and git's default of 7 as well),
alone or followed by a space and a label — and reports the path when some
marker line occurs more often than in either parent's version. A marker
line a parent already carries (a test fixture, a doc about conflicts) is
not residue. Exit 0 and no output when there is none; exit 1 with one
`<path>:<line>` per residue line (the first extra occurrence of each);
exit 2 when git fails. Runs git with whatever pins the caller exported.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter

DEFAULT_SIZE = 7


class GitError(Exception):
    pass


def git(*args: str, stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(["git", *args], input=stdin, capture_output=True, check=False)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def blobs(rev: str) -> dict[bytes, bytes]:
    """path -> blob sha of every regular file in `rev` (`--cached`: the index, stage 0)."""
    out: dict[bytes, bytes] = {}
    if rev == "--cached":
        for entry in git("ls-files", "-s", "-z").split(b"\0"):
            if not entry:
                continue
            meta, path = entry.split(b"\t", 1)
            mode, sha, stage = meta.split(b" ")
            if stage == b"0" and mode != b"160000":
                out[path] = sha
    else:
        for entry in git("ls-tree", "-r", "-z", "--full-tree", rev).split(b"\0"):
            if not entry:
                continue
            meta, path = entry.split(b"\t", 1)
            mode, kind, sha = meta.split(b" ")
            if kind == b"blob":
                out[path] = sha
    return out


def contents(shas: set[bytes]) -> dict[bytes, bytes]:
    """blob sha -> raw content, through one `git cat-file --batch` (no filters, no textconv)."""
    if not shas:
        return {}
    order = sorted(shas)
    raw = git("cat-file", "--batch", stdin=b"".join(s + b"\n" for s in order))
    out: dict[bytes, bytes] = {}
    pos = 0
    for sha in order:
        header_end = raw.index(b"\n", pos)
        header = raw[pos:header_end].split(b" ")
        if len(header) != 3 or header[1] != b"blob":
            raise GitError(f"git cat-file: unexpected {raw[pos:header_end]!r}")
        size = int(header[2])
        start = header_end + 1
        out[sha] = raw[start : start + size]
        pos = start + size + 1
    return out


def marker_sizes(paths: list[bytes], cached: bool) -> dict[bytes, set[int]]:
    """path -> the marker sizes to look for: its conflict-marker-size attribute, and 7."""
    sizes = {p: {DEFAULT_SIZE} for p in paths}
    if not paths:
        return sizes
    args = ["check-attr", "-z", "--stdin"] + (["--cached"] if cached else []) + ["conflict-marker-size"]
    fields = git(*args, stdin=b"".join(p + b"\0" for p in paths)).split(b"\0")
    for i in range(0, len(fields) - 2, 3):
        path, _, value = fields[i : i + 3]
        if value.isdigit() and 0 < int(value) <= 1000:
            sizes.setdefault(path, {DEFAULT_SIZE}).add(int(value))
    return sizes


def marker_lines(data: bytes, sizes: set[int]) -> list[tuple[int, bytes]]:
    """(line number, line) for each conflict-marker line of `data`."""
    alts = []
    for n in sorted(sizes):
        for char in (b"<", b">", b"|"):
            c = re.escape(char)
            alts.append(rb"%s{%d}(?!%s)(?: .*)?" % (c, n, c))
        alts.append(rb"={%d}(?!=)" % n)
    pattern = re.compile(rb"^(?:" + rb"|".join(alts) + rb")$")
    found = []
    for number, line in enumerate(data.split(b"\n"), 1):
        line = line.rstrip(b"\r")
        if pattern.match(line):
            found.append((number, line))
    return found


def residue(parent1: str, parent2: str, result: str) -> list[str]:
    trees = [blobs(parent1), blobs(parent2)]
    final = blobs(result)
    changed = sorted(p for p, sha in final.items() if any(t.get(p) != sha for t in trees))
    sizes = marker_sizes(changed, result == "--cached")
    wanted = {final[p] for p in changed} | {t[p] for p in changed for t in trees if p in t}
    data = contents(wanted)
    report: list[str] = []
    for path in changed:
        mine = marker_lines(data[final[path]], sizes[path])
        if not mine:
            continue
        theirs = [Counter(line for _, line in marker_lines(data[t[path]], sizes[path])) for t in trees if path in t]
        allowed = Counter()
        for counts in theirs:
            allowed |= counts
        seen: Counter = Counter()
        for number, line in mine:
            seen[line] += 1
            if seen[line] > allowed[line]:
                report.append(f"{path.decode(errors='replace')}:{number}")
    return report


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: conflict_residue.py <parent1> <parent2> <--cached | result-commit>", file=sys.stderr)
        return 2
    try:
        found = residue(*argv)
    except (GitError, ValueError) as exc:
        print(f"conflict_residue: {exc}", file=sys.stderr)
        return 2
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
