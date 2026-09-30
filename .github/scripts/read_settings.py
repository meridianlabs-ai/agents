#!/usr/bin/env python3
"""Read the settings file a caller's `settings` input names (Claude Security
finding 4773338).

The `compose-settings` composite runs this as the runner, before the agent
user exists, when the input is not a JSON object. The file is the
checkout's, which on a PR run is the PR head, so a committed symlink used
to redirect the read to any file the runner can read. This reader:

- takes the path relative to `$GITHUB_WORKSPACE` (an absolute path must be
  inside it) and refuses a `..` component;
- walks it one component at a time from the workspace, opening each
  directory `O_NOFOLLOW` and refusing a symlink at any step;
- opens the file `O_NOFOLLOW`, requires a regular file, and refuses one
  over `MAX_BYTES` or that is not UTF-8 text.

On success it writes the file's text to stdout. On refusal it prints an
`::error::` line to stderr, writes nothing to stdout, and exits 1.
Stdlib only.
"""

import os
import stat
import sys

MAX_BYTES = 256 * 1024


class Refused(Exception):
    pass


def _parts(value: str, workspace: str) -> list[str]:
    if not value:
        raise Refused("is empty")
    if "\0" in value:
        raise Refused("contains a NUL byte")
    if ".." in value.split("/"):
        raise Refused("has a '..' component")
    path = os.path.normpath(os.path.join(workspace, value))
    rel = os.path.relpath(path, workspace)
    if rel == "." or rel == ".." or rel.startswith("../") or os.path.isabs(rel):
        raise Refused("is not a file inside the workspace")
    return rel.split("/")


def read_settings(value: str, workspace: str, max_bytes: int = MAX_BYTES) -> str:
    """Return the text of the file VALUE names under WORKSPACE, or raise
    Refused."""
    workspace = os.path.abspath(workspace)
    parts = _parts(value, workspace)
    dfd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        walked = []
        for name in parts[:-1]:
            walked.append(name)
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                raise Refused("does not exist") from None
            if stat.S_ISLNK(st.st_mode):
                raise Refused(f"has a symlink at {'/'.join(walked)}")
            if not stat.S_ISDIR(st.st_mode):
                raise Refused(f"has a non-directory at {'/'.join(walked)}")
            try:
                nfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
            except OSError as e:
                raise Refused(f"cannot open {'/'.join(walked)}: {e.strerror}") from None
            os.close(dfd)
            dfd = nfd
        try:
            st = os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            raise Refused("does not exist") from None
        if stat.S_ISLNK(st.st_mode):
            raise Refused("is a symlink")
        try:
            # O_NONBLOCK so a FIFO cannot hang the open; fstat refuses it next.
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY, dir_fd=dfd)
        except OSError as e:
            raise Refused(f"cannot be opened: {e.strerror}") from None
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused("is not a regular file")
        if st.st_size > max_bytes:
            raise Refused(f"is larger than {max_bytes} bytes")
        chunks = []
        size = 0
        while size <= max_bytes:
            chunk = os.read(fd, max_bytes + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > max_bytes:
            raise Refused(f"is larger than {max_bytes} bytes")
    finally:
        os.close(fd)
    data = b"".join(chunks)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("is not UTF-8 text") from None
    if "\0" in text:
        raise Refused("contains a NUL byte")
    return text


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("::error::usage: read_settings.py <path>", file=sys.stderr)
        return 2
    value = argv[1]
    workspace = os.environ.get("GITHUB_WORKSPACE", "")
    if not workspace:
        print("::error::read_settings.py: GITHUB_WORKSPACE is not set", file=sys.stderr)
        return 2
    try:
        text = read_settings(value, workspace)
    except Refused as e:
        print(f"::error::the settings input is not a JSON object, and the path {value!r} {e}; "
              "a settings path must name a regular file inside the workspace, with no symlink on the way",
              file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
