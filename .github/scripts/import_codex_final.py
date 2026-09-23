#!/usr/bin/env python3
"""Copy codex's final message across the user boundary (`import-codex-final`).

codex-action writes the final message where the caller's `output-file` says,
`$RUNNER_TEMP/codex/codex-final.md`, and that directory is codex-owned: the
codex CLI runs as the unprivileged `codex` user (`sudo -u codex -- codex exec
--output-last-message FILE`), so the file it creates is codex's and so is
the directory entry. Every later step runs as `runner` and used to open
that entry with symlink-following primitives (`[ -f ]`, awk, sed, head), so
a codex that replaced the file with a symlink — a prompt-injected codex, via
a background survivor that swaps the entry after the CLI wrote it and before
the reclaim step's `pkill -u codex` — would have the runner read the TARGET
and the land job publish it on the PR as the machine account: the reading
step's environment, runner-home files codex cannot read (finding 4628447).

This script is the one place the codex-owned entry is opened, and it does
so without following anything: `O_NOFOLLOW` on the open (a symlink at the
path fails with ELOOP; a dangling one too), `fstat` on the descriptor it got
(never a second lookup by name — no window between check and read), refusing
anything that is not a regular file (a directory, a FIFO — opened
`O_NONBLOCK` so it cannot block the job — a device) or that the expected
user does not own (a hard link keeps its target's owner, so a link to a
runner file fails here even where `fs.protected_hardlinks` is off). What
passes is copied into a runner-owned file directly under `$RUNNER_TEMP`
(755 `runner:runner`, no codex entry possible), and every later reader —
the commit subject, `resolve-reported-threads`, the summary body — reads
only that copy. A refusal is a `::warning::` and NO copy, never a failure:
the callers' readers already fall back to the default commit subject and
the "(codex produced no final message)" placeholder when the copy is
missing, which is exactly what a redirected message must yield. Only a
misconfiguration (an unknown owner, an unwritable destination) exits
non-zero.

Stdlib only, like validate_manifest.py; the composite runs it with the
runner's python3.
"""

from __future__ import annotations

import argparse
import errno
import os
import pwd
import stat
import sys

# Well above any real final message (the callers cap the posted body at
# 59,000 bytes and the commit subject at 120 characters), low enough that a
# codex filling the disk cannot make the copy do the same.
DEFAULT_MAX_BYTES = 1 << 20


def warn(msg: str) -> None:
    print(f"::warning::{msg}", flush=True)


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def read_verified(source: str, owner_uid: int, max_bytes: int) -> bytes | None:
    """The source's bytes when it is a regular file owned by OWNER_UID and not a
    symlink; None (after a `::warning::`) otherwise, None silently when there
    is no entry at all."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(source, flags)
    except FileNotFoundError:
        print(f"no codex final message at {source}; nothing imported.")
        return None
    except OSError as e:
        if e.errno == errno.ELOOP:
            warn(f"refusing codex final message at {source}: it is a symlink (codex-owned directory entry redirected); nothing imported")
        else:
            warn(f"refusing codex final message at {source}: cannot open it ({e.strerror}); nothing imported")
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            warn(f"refusing codex final message at {source}: not a regular file (mode {stat.filemode(st.st_mode)}); nothing imported")
            return None
        if st.st_uid != owner_uid:
            warn(f"refusing codex final message at {source}: owned by uid {st.st_uid}, expected uid {owner_uid}; nothing imported")
            return None
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        warn(f"codex final message at {source} exceeds {max_bytes} bytes; the imported copy is truncated to that")
        data = data[:max_bytes]
    return data


def write_copy(dest: str, data: bytes) -> None:
    """Write DATA to DEST through a fresh temporary entry and an atomic rename;
    O_EXCL|O_NOFOLLOW so a pre-existing name is never followed or reused."""
    tmp = dest + ".tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except BaseException:
        os.unlink(tmp)
        raise
    os.replace(tmp, dest)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source", required=True, help="codex-action's output-file, in the codex-owned directory")
    p.add_argument("--dest", required=True, help="runner-owned path to write the verified copy to")
    p.add_argument("--owner", required=True, help="user name the source must be owned by (the codex user)")
    p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = p.parse_args(argv)

    try:
        owner_uid = pwd.getpwnam(args.owner).pw_uid
    except KeyError:
        print(f"::error::import-codex-final: no such user {args.owner!r}; cannot verify who owns the final message", flush=True)
        return 2

    # A stale copy from an earlier run of this step must never stand in for
    # a message refused now. unlink never follows a symlink.
    try:
        os.unlink(args.dest)
    except FileNotFoundError:
        pass

    data = read_verified(args.source, owner_uid, args.max_bytes)
    if data is None:
        set_output("path", "")
        return 0
    write_copy(args.dest, data)
    set_output("path", args.dest)
    print(f"imported codex final message: {len(data)} bytes from {args.source} to {args.dest} (regular file owned by {args.owner}, not a symlink).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
