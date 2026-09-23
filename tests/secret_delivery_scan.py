#!/usr/bin/env python3
"""Does this job's runner hold a given secret in memory? (Claude Security
finding 4629153; .github/workflows/engine-isolation-canary.yml.)

Run as root on a hosted runner, from a step that itself references no
secret. Reads the memory of every Runner.Worker and Runner.Listener process
through /proc/<pid>/mem and counts occurrences of sentinel values — synthetic
repository secrets of the form CANARYA-<16 hex> and CANARYB-<16 hex>, whose
prefixes are assembled here at runtime so the contiguous needle never appears
in this file (a file whose text sits in the job message would otherwise match
itself). Both UTF-8 and UTF-16LE encodings are searched: the job message
arrives as UTF-8 JSON and is deserialised into .NET strings (UTF-16), and the
secret masker keeps the values as strings too.

Usage: secret_delivery_scan.py EXPECT_A EXPECT_B   (each `present` or `absent`)

Prints one line per process and needle with the match count, never the
matched bytes, and exits non-zero when an expectation is not met — so a job
that expects `present` is the positive control (the detection works) and a
job that expects `absent` is the guarantee under test.
"""
import re
import sys
from pathlib import Path

NEEDLES = {
    "A": re.compile(("CANARY" + "A-").encode() + rb"[0-9a-f]{16}"),
    "B": re.compile(("CANARY" + "B-").encode() + rb"[0-9a-f]{16}"),
}
NEEDLES_UTF16 = {
    k: re.compile(("CANARY" + k + "-").encode("utf-16-le") + rb"(?:[0-9a-f]\x00){16}") for k in NEEDLES
}


def runner_processes():
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmdline = (p / "cmdline").read_bytes().decode(errors="replace")
        except OSError:
            continue
        for name in ("Runner.Worker", "Runner.Listener"):
            if name in cmdline:
                yield int(p.name), name


def scan(pid):
    counts = {k: 0 for k in NEEDLES}
    regions = 0
    with open(f"/proc/{pid}/maps") as maps, open(f"/proc/{pid}/mem", "rb", 0) as mem:
        for line in maps:
            fields = line.split()
            addr, perms = fields[0], fields[1]
            if "r" not in perms or (len(fields) > 5 and fields[5] in ("[vvar]", "[vsyscall]")):
                continue
            start, end = (int(x, 16) for x in addr.split("-"))
            if end - start > 1 << 31:
                continue
            try:
                mem.seek(start)
                data = mem.read(end - start)
            except (OSError, ValueError, OverflowError):
                continue
            regions += 1
            for k in NEEDLES:
                counts[k] += len(NEEDLES[k].findall(data)) + len(NEEDLES_UTF16[k].findall(data))
    return regions, counts


def main():
    expect = {"A": sys.argv[1], "B": sys.argv[2]}
    total = {"A": 0, "B": 0}
    procs = list(runner_processes())
    if not procs:
        print("::error::no Runner.Worker / Runner.Listener process found")
        return 2
    for pid, name in procs:
        regions, counts = scan(pid)
        print(f"{name} pid {pid}: {regions} readable regions; sentinel A x{counts['A']}, sentinel B x{counts['B']}")
        for k in total:
            total[k] += counts[k]
    rc = 0
    for k, want in expect.items():
        found = total[k] > 0
        ok = found == (want == "present")
        print(f"sentinel {k}: {'found' if found else 'not found'} in runner memory; expected {want} -> {'ok' if ok else 'MISMATCH'}")
        rc = rc or (0 if ok else 1)
    return rc


if __name__ == "__main__":
    sys.exit(main())
