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

`--mode dir` does the same for a whole directory: the landing directory
the Claude agent writes into as `claude-agent`, `$RUNNER_TEMP/claude-agent`
(design/executed-paths-residual.md → After the agent). It opens the
directory once (`O_DIRECTORY|O_NOFOLLOW`) and every entry relative to that
descriptor, and copies each entry that is a regular file owned by the
expected user with exactly one link (a hard link to a file elsewhere is
refused whoever owns it), named `[A-Za-z0-9._-]+` without a leading dot, and
no larger than the per-file cap into a freshly created runner-only
directory. Anything else — a symlink, a subdirectory, a FIFO, a foreign
owner, a hard link, a bad name, an unreadable or oversize file — is skipped
with a `::warning::` (never truncated: a cut-off manifest is worse than
none). When the entries that pass exceed the aggregate byte cap or the file
count cap, the whole import is refused and no destination is created,
rather than copying a subset the reader cannot tell from the whole.

Stdlib only, like validate_manifest.py; the composite runs it with the
runner's python3.
"""

from __future__ import annotations

import argparse
import errno
import os
import pwd
import re
import shutil
import stat
import sys

# Well above any real final message (the callers cap the posted body at
# 59,000 bytes and the commit subject at 120 characters), low enough that a
# codex filling the disk cannot make the copy do the same.
DEFAULT_MAX_BYTES = 1 << 20
# Directory mode: the per-file cap is DEFAULT_MAX_BYTES (the land
# validator's manifest cap; its body files are capped at 64 KiB), and the
# whole import at this many bytes and files — well above a manifest plus its
# comment bodies, or a review and its inline findings.
DEFAULT_MAX_TOTAL_BYTES = 8 << 20
DEFAULT_MAX_FILES = 256
SAFE_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")


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


def read_entry(dir_fd: int, name: str, label: str, owner_uid: int, max_bytes: int) -> bytes | None:
    """One entry of the source directory, opened relative to DIR_FD without
    following anything: its bytes when it is a regular, single-link file owned
    by OWNER_UID within MAX_BYTES; None after a `::warning::` otherwise."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError as e:
        if e.errno == errno.ELOOP:
            warn(f"skipping {label}: it is a symlink")
        else:
            warn(f"skipping {label}: cannot open it ({e.strerror})")
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            warn(f"skipping {label}: not a regular file (mode {stat.filemode(st.st_mode)})")
            return None
        if st.st_uid != owner_uid:
            warn(f"skipping {label}: owned by uid {st.st_uid}, expected uid {owner_uid}")
            return None
        if st.st_nlink != 1:
            warn(f"skipping {label}: it has {st.st_nlink} links (a hard link)")
            return None
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    except OSError as e:
        warn(f"skipping {label}: cannot read it ({e.strerror})")
        return None
    finally:
        os.close(fd)
    if total > max_bytes:
        warn(f"skipping {label}: it exceeds {max_bytes} bytes")
        return None
    return b"".join(chunks)


def import_dir(source: str, dest: str, owner_uid: int, max_bytes: int, max_total: int, max_files: int) -> int | None:
    """Copy the passing entries of SOURCE into a fresh DEST (see the module
    docstring); the number copied, or None when nothing was imported."""
    try:
        dir_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        print(f"no landing directory at {source}; nothing imported.")
        return None
    except OSError as e:
        why = "it is a symlink" if e.errno == errno.ELOOP else f"cannot open it as a directory ({e.strerror})"
        warn(f"refusing landing directory {source}: {why}; nothing imported")
        return None
    try:
        files: dict[str, bytes] = {}
        total = 0
        for name in sorted(os.listdir(dir_fd)):
            # ascii(): a name is the agent's to choose, and a newline in it
            # would start a workflow command in the log.
            label = f"{source}/{ascii(name)[1:-1]}"
            if not SAFE_NAME.fullmatch(name):
                warn(f"skipping {label}: the name is not [A-Za-z0-9._-]+ without a leading dot")
                continue
            data = read_entry(dir_fd, name, label, owner_uid, max_bytes)
            if data is None:
                continue
            files[name] = data
            total += len(data)
            if len(files) > max_files or total > max_total:
                warn(f"refusing landing directory {source}: more than {max_files} files or {max_total} bytes; nothing imported")
                return None
    finally:
        os.close(dir_fd)
    os.mkdir(dest, 0o700)
    for name, data in files.items():
        fd = os.open(os.path.join(dest, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    return len(files)


def remove_stale(dest: str) -> None:
    """Remove DEST, never following it: a symlink or file is unlinked, a
    directory removed with its contents."""
    try:
        st = os.lstat(dest)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(dest)
    else:
        os.unlink(dest)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--mode", choices=("file", "dir"), default="file",
                   help="file: one final message (the default); dir: every entry of a landing directory")
    p.add_argument("--source", required=True, help="codex-action's output-file, in the codex-owned directory; with --mode dir, the agent-owned landing directory")
    p.add_argument("--dest", required=True, help="runner-owned path to write the verified copy to; with --mode dir, the directory to create")
    p.add_argument("--owner", required=True, help="user name the source must be owned by (the codex user)")
    p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES, help="--mode dir: cap on the whole import")
    p.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES, help="--mode dir: cap on the number of files")
    args = p.parse_args(argv)

    try:
        owner_uid = pwd.getpwnam(args.owner).pw_uid
    except KeyError:
        print(f"::error::import-codex-final: no such user {args.owner!r}; cannot verify who owns the final message", flush=True)
        return 2

    if args.mode == "dir":
        # A stale copy must never stand in for an import refused now.
        remove_stale(args.dest)
        count = import_dir(args.source, args.dest, owner_uid, args.max_bytes, args.max_total_bytes, args.max_files)
        if count is None:
            set_output("path", "")
            return 0
        set_output("path", args.dest)
        print(f"imported {count} file(s) from {args.source} to {args.dest} (regular single-link files owned by {args.owner}, not symlinks).")
        return 0

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
