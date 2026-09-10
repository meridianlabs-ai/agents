#!/usr/bin/env python3
"""Validate a landing manifest before the land job acts on it.

The manifest is the trust boundary between the untrusted agent job and the
trusted land job (design/architecture.md → "Landing job"). The agent job —
where the agent, its subagents, and anything it left running had a shell —
writes `manifest.json` plus body files into an artifact; the land job, on a
fresh runner with the privileged token, downloads that artifact and runs
this validator FIRST, before any network call and before any field is read
into a shell variable. Everything here fails closed: an unknown key, a
wrong type, an out-of-range value or an escaping file reference is a
violation, and one violation is enough to refuse the whole manifest.

Stdlib only — the land job installs nothing. Prints one line per violation
to stdout and exits 1 when there is at least one; exits 0 on a clean
manifest. Usage:

    validate_manifest.py --dir "$RUNNER_TEMP/landing" \
        --repo owner/name --run-id "$GITHUB_RUN_ID" --default-branch meridian \
        --refused-branches main \
        --allowed-issue-repos owner/name,owner/other \
        --event-pr-number "$EVENT_PR" --event-issue-number "$EVENT_ISSUE" \
        [--pr-head-ref <headRefName of that PR, from the API>] \
        [--branch-prefix "claude/issue-$EVENT_ISSUE-"] [--refuse-bundle]

`--refuse-bundle` is for callers whose agent never commits (the reviewer):
a manifest that carries commits, claims HEAD moved, or ships a
`commits.bundle` is refused, so that land job can never become a push
channel however the artifact was produced.

`--event-pr-number` / `--event-issue-number` are the numbers the run's
TRUSTED context names (the event payload, or a gate-job output computed
before any untrusted code ran); the manifest's `pr_number` / `issue_number`
must equal them exactly — null when the event names none — so an agent job
cannot steer the landing (push, replies, thread resolutions, hand-back) at a
PR of its choosing. On a PR run that also pins `branch` to that PR's live
head ref. On a run that names no PR, `branch` is agent-chosen unless
`--branch-prefix` is set: then it must start with the prefix (the caller
composes it from trusted context — an issue run's `claude/issue-N-`), which
keeps the push off the branches of PRs opened for other issues (an earlier
run's still-open PR for the same issue carries the same prefix; reaching it
is the adopt path, not a breach).

The schema is documented in .github/actions/emit-landing/README.md; keep
the two in step (an added field must be added to KNOWN_TOP_LEVEL here and
to the README, or every manifest carrying it is rejected).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024  # 1 MiB
MAX_BODY_FILE_BYTES = 64 * 1024  # 64 KiB (GitHub caps comments at 65,536 chars)
MAX_TITLE_CHARS = 256
MAX_MESSAGE_CHARS = MAX_BODY_FILE_BYTES

BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
THREAD_RE = re.compile(r"^PRRT_[A-Za-z0-9_-]+$")
FILE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
# The event-payload numbers the land job passes through: digits, no leading
# zero, and short enough that a GitHub expression's empty string is the only
# other value they ever take.
EVENT_NUMBER_RE = re.compile(r"^[1-9][0-9]{0,9}$")

# Atlas Stage options (.github/actions/set-stage/action.yml).
STAGES = ("Contributor", "Agent", "Review", "Sign-off", "Merge")

KNOWN_TOP_LEVEL = {
    "schema",
    "repo",
    "run_id",
    "branch",
    "start_sha",
    "head_sha",
    "has_bundle",
    "pr_number",
    "issue_number",
    "pr",
    "comments",
    "replies",
    "resolve_threads",
    "issues",
    "stage",
    "handback",
    "handoff_body_file",
    "error",
    "provenance_comment_file",
    "review_verdict",
}
# The reviewer's verdict marker comment (claude-review.yml's codex path): the
# land job posts one of two FIXED bodies chosen by this value, so the loop's
# markers stay live without any agent text passing the de-fang un-broken.
VERDICTS = ("clean", "suggestions")
KNOWN_PR = {"open", "title", "body_file", "base", "labels", "issue"}
# No `target`: the issues endpoint serves PRs and issues alike, so `number`
# is all the land job needs; a field it never reads would only mislead.
KNOWN_COMMENT = {"number", "body_file"}
KNOWN_REPLY = {"review_comment_id", "body_file"}
KNOWN_ISSUE = {"repo", "title", "body_file", "labels", "comment_on"}
KNOWN_ERROR = {"message", "fail_run"}


def _is_int(value) -> bool:
    # bool is a subclass of int; `true` must not pass as a number.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_positive_int(value) -> bool:
    return _is_int(value) and value > 0


class Validator:
    def __init__(
        self,
        manifest,
        *,
        artifact_dir: Path,
        repo: str,
        run_id: str,
        default_branch: str,
        allowed_issue_repos,
        pr_head_ref: str = "",
        refused_branches=(),
        event_pr_number: str = "",
        event_issue_number: str = "",
        branch_prefix: str = "",
        refuse_bundle: bool = False,
    ) -> None:
        self.m = manifest
        self.dir = Path(artifact_dir)
        self.repo = repo
        self.run_id = str(run_id)
        self.default_branch = default_branch
        # Branch names are case-sensitive refs: compared exactly, unlike repos.
        self.refused_branches = {b.strip() for b in refused_branches if b.strip()}
        self.allowed_issue_repos = {r.strip().lower() for r in allowed_issue_repos if r.strip()}
        self.pr_head_ref = pr_head_ref
        self.event_pr_number = event_pr_number.strip()
        self.event_issue_number = event_issue_number.strip()
        self.branch_prefix = branch_prefix.strip()
        self.refuse_bundle = refuse_bundle
        self.errors: list[str] = []

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    # -- helpers -----------------------------------------------------------

    def _unknown_keys(self, obj: dict, known: set, where: str) -> None:
        for key in sorted(set(obj) - known):
            self.err(f"{where}: unknown key {key!r} (fail closed on schema drift)")

    def _str(self, obj: dict, key: str, where: str, *, required: bool, max_len: int | None = None) -> str | None:
        if key not in obj or obj[key] is None:
            if required:
                self.err(f"{where}: {key} is required")
            return None
        value = obj[key]
        if not isinstance(value, str):
            self.err(f"{where}: {key} must be a string")
            return None
        if required and not value:
            self.err(f"{where}: {key} must not be empty")
            return None
        if max_len is not None and len(value) > max_len:
            self.err(f"{where}: {key} exceeds {max_len} characters")
        return value

    def _bool(self, obj: dict, key: str, where: str, *, required: bool) -> bool | None:
        if key not in obj or obj[key] is None:
            if required:
                self.err(f"{where}: {key} is required")
            return None
        if not isinstance(obj[key], bool):
            self.err(f"{where}: {key} must be a boolean")
            return None
        return obj[key]

    def _positive_int(self, obj: dict, key: str, where: str, *, required: bool) -> int | None:
        if key not in obj or obj[key] is None:
            if required:
                self.err(f"{where}: {key} is required")
            return None
        if not _is_positive_int(obj[key]):
            self.err(f"{where}: {key} must be a positive integer")
            return None
        return obj[key]

    def _file_ref(self, obj: dict, key: str, where: str, *, required: bool) -> None:
        ref = self._str(obj, key, where, required=required)
        if ref is None:
            return
        label = f"{where}: {key} {ref!r}"
        if not ref or ref.startswith("/"):
            self.err(f"{label} must be a relative path inside the artifact")
            return
        # A tight charset: the land job reads these names line by line from
        # jq output, so a newline (or any other oddity) in a name must never
        # get that far.
        if not FILE_REF_RE.match(ref):
            self.err(f"{label} has characters outside [A-Za-z0-9._/-] or is longer than 200")
            return
        parts = ref.split("/")
        if any(part in ("", ".", "..") for part in parts):
            self.err(f"{label} must not contain '..', '.' or empty path components")
            return
        # emit-landing uploads with include-hidden-files false, so a
        # dot-named file or directory never reaches the land job; refuse the
        # reference here, where the cause is visible, rather than let the
        # existence check below report "does not exist" for a file the
        # workflow did write.
        if any(part.startswith(".") for part in parts):
            self.err(f"{label} must not have a path component starting with '.' (hidden files are not uploaded)")
            return
        # Walk the components with lstat so a symlink ANYWHERE in the path is
        # caught — not just at the leaf — without following it.
        current = self.dir
        for part in parts:
            current = current / part
            try:
                st = current.lstat()
            except FileNotFoundError:
                self.err(f"{label} does not exist")
                return
            except OSError as exc:
                self.err(f"{label} cannot be read: {exc.strerror}")
                return
            if os.path.islink(current) or (st.st_mode & 0o170000) == 0o120000:
                self.err(f"{label} is (or passes through) a symlink")
                return
        if not current.is_file():
            self.err(f"{label} is not a regular file")
            return
        try:
            resolved = current.resolve(strict=True)
            root = self.dir.resolve(strict=True)
        except OSError as exc:
            self.err(f"{label} cannot be resolved: {exc}")
            return
        if root != resolved and root not in resolved.parents:
            self.err(f"{label} resolves outside the artifact directory")
            return
        if st.st_size > MAX_BODY_FILE_BYTES:
            self.err(f"{label} is {st.st_size} bytes; the cap is {MAX_BODY_FILE_BYTES}")

    def _tie_to_event(self, key: str, value: int | None, event: str, what: str, flag: str) -> None:
        """Pin `pr_number` / `issue_number` to the number the run's event names.

        Both sides come from the same trusted expression in the caller (the
        event payload, or a gate-job output) — the agent job only copies it
        into the manifest — so the two must agree exactly, null included: a
        manifest may neither name a PR/issue the event did not, nor drop the
        one it did (which would let `pr.open` adopt an arbitrary open PR).
        """
        if event and not EVENT_NUMBER_RE.match(event):
            self.err(f"manifest: {flag} {event!r} is not a positive integer (caller misconfiguration; failing closed)")
            return
        if value is None:
            if event and self.m.get(key) is None:
                self.err(f"manifest: {key} is null but this run's event names {what} #{event}")
            return
        if not event:
            self.err(f"manifest: {key} is set ({value}) but this run's event names no {what} ({flag} is empty)")
        elif str(value) != event:
            self.err(f"manifest: {key} {value} is not the {what} this run's event names (#{event})")

    def _labels(self, obj: dict, where: str) -> None:
        if "labels" not in obj or obj["labels"] is None:
            return
        labels = obj["labels"]
        if not isinstance(labels, list) or not all(isinstance(x, str) and 0 < len(x) <= 50 for x in labels):
            self.err(f"{where}: labels must be a list of non-empty strings (≤ 50 chars)")

    # -- rules -------------------------------------------------------------

    def run(self) -> list[str]:
        m = self.m
        if not isinstance(m, dict):
            self.err("manifest: top level must be a JSON object")
            return self.errors
        self._unknown_keys(m, KNOWN_TOP_LEVEL, "manifest")

        if m.get("schema") != SCHEMA_VERSION or not _is_int(m.get("schema")):
            self.err(f"manifest: schema must be {SCHEMA_VERSION}")

        repo = self._str(m, "repo", "manifest", required=True)
        if repo is not None and repo.lower() != self.repo.lower():
            self.err(f"manifest: repo {repo!r} is not the repo this land job operates on ({self.repo!r})")

        run_id = m.get("run_id")
        if not _is_positive_int(run_id) or str(run_id) != self.run_id:
            self.err(f"manifest: run_id {run_id!r} does not match this run ({self.run_id})")

        # The default branch is what the branch rule compares against; an
        # empty value (a caller event without a `repository` payload, and no
        # lookup) would silently skip that rule, so it fails closed instead.
        if not self.default_branch:
            self.err("manifest: no default branch was supplied to compare against (--default-branch is empty)")

        branch = self._str(m, "branch", "manifest", required=True)
        pr = m.get("pr")
        pr_base = None
        if pr is not None:
            if not isinstance(pr, dict):
                self.err("manifest: pr must be an object")
                pr = None
        if branch is not None:
            if not BRANCH_RE.match(branch):
                self.err("manifest: branch has characters outside [A-Za-z0-9._/-] or is longer than 200")
            if ".." in branch:
                self.err("manifest: branch must not contain '..'")
            if branch.startswith("refs/"):
                self.err("manifest: branch must be a bare branch name, not a ref (starts with 'refs/')")
            if branch.startswith("/") or branch.endswith("/") or branch.endswith(".lock"):
                self.err("manifest: branch must not start or end with '/' or end with '.lock'")
            if branch == self.default_branch:
                self.err(f"manifest: branch must not be the default branch ({self.default_branch!r})")
            # Beyond the default branch: names the land job refuses outright
            # (`main` by default), so a pristine mirror that is not the
            # default — the inspect_ai fork's `main` under `meridian` — is
            # refused here, before any network call, and not only by its
            # server-side ruleset.
            if branch in self.refused_branches:
                self.err(f"manifest: branch {branch!r} is on the land job's refused list ({', '.join(sorted(self.refused_branches))})")

        # pr_number is pinned to the PR the run's event names (so the head-ref
        # rule below compares against THAT PR's branch, not one the agent
        # picked), and issue_number to the event's issue, the same way.
        pr_number = self._positive_int(m, "pr_number", "manifest", required=False)
        self._tie_to_event("pr_number", pr_number, self.event_pr_number, "PR", "--event-pr-number")
        if pr_number is not None:
            if not self.pr_head_ref:
                self.err("manifest: pr_number is set but no PR head ref was supplied to compare against (--pr-head-ref)")
            elif branch is not None and branch != self.pr_head_ref:
                self.err(f"manifest: branch {branch!r} is not PR #{pr_number}'s head ref ({self.pr_head_ref!r})")
        elif self.branch_prefix and branch is not None and not branch.startswith(self.branch_prefix):
            # No PR to pin the branch to (an issue run): without this the
            # agent job could name another issue's open PR head, bundle on its tip
            # and fast-forward it — the push is non-force and never the
            # default branch, but it would land on a PR of the agent's
            # choosing and `pr.open` would adopt it. The caller composes the
            # prefix from trusted context (`claude/issue-N-` for issue N).
            self.err(f"manifest: branch {branch!r} does not start with the prefix this run's branches must carry ({self.branch_prefix!r}; --branch-prefix)")

        issue_number = self._positive_int(m, "issue_number", "manifest", required=False)
        self._tie_to_event("issue_number", issue_number, self.event_issue_number, "issue", "--event-issue-number")

        start_sha = self._str(m, "start_sha", "manifest", required=True)
        head_sha = self._str(m, "head_sha", "manifest", required=True)
        for name, sha in (("start_sha", start_sha), ("head_sha", head_sha)):
            if sha is not None and not SHA_RE.match(sha):
                self.err(f"manifest: {name} must be 40 lowercase hex characters")
        has_bundle = self._bool(m, "has_bundle", "manifest", required=True)
        if has_bundle is not None and start_sha and head_sha:
            if has_bundle and head_sha == start_sha:
                self.err("manifest: has_bundle is true but head_sha equals start_sha")
            if not has_bundle and head_sha != start_sha:
                self.err("manifest: has_bundle is false but head_sha differs from start_sha")
        if has_bundle:
            bundle = self.dir / "commits.bundle"
            if bundle.is_symlink() or not bundle.is_file():
                self.err("manifest: has_bundle is true but commits.bundle is missing or not a regular file")
        if self.refuse_bundle:
            # The caller's agent never commits (the reviewer: contents:read,
            # git commit denied). Its land job must never become a push
            # channel, so a manifest that carries commits — or merely claims
            # HEAD moved, or ships a bundle file — is refused outright,
            # whatever the agent job (or anything running in it) uploaded.
            if has_bundle or (start_sha and head_sha and head_sha != start_sha):
                self.err("manifest: this land job refuses bundles (--refuse-bundle) but the manifest carries commits (has_bundle / head_sha != start_sha)")
            if (self.dir / "commits.bundle").exists() or (self.dir / "commits.bundle").is_symlink():
                self.err("manifest: this land job refuses bundles (--refuse-bundle) but commits.bundle is present in the artifact")

        if pr is not None:
            self._unknown_keys(pr, KNOWN_PR, "pr")
            self._bool(pr, "open", "pr", required=True)
            self._str(pr, "title", "pr", required=True, max_len=MAX_TITLE_CHARS)
            self._file_ref(pr, "body_file", "pr", required=True)
            # Optional: absent or empty means the land job's default branch
            # (which `branch` already may not equal), as `gh pr create`
            # defaults when claude.yml passes no --base.
            pr_base = self._str(pr, "base", "pr", required=False) or None
            if pr_base is not None:
                if not BRANCH_RE.match(pr_base):
                    self.err("pr: base has characters outside [A-Za-z0-9._/-] or is longer than 200")
                if branch is not None and pr_base == branch:
                    self.err("manifest: branch must not equal pr.base")
            self._labels(pr, "pr")
            self._positive_int(pr, "issue", "pr", required=False)

        comments = m.get("comments")
        if comments is not None:
            if not isinstance(comments, list):
                self.err("manifest: comments must be a list")
            else:
                for i, c in enumerate(comments):
                    where = f"comments[{i}]"
                    if not isinstance(c, dict):
                        self.err(f"{where}: must be an object")
                        continue
                    self._unknown_keys(c, KNOWN_COMMENT, where)
                    self._positive_int(c, "number", where, required=True)
                    self._file_ref(c, "body_file", where, required=True)

        replies = m.get("replies")
        if replies is not None:
            if not isinstance(replies, list):
                self.err("manifest: replies must be a list")
            else:
                for i, r in enumerate(replies):
                    where = f"replies[{i}]"
                    if not isinstance(r, dict):
                        self.err(f"{where}: must be an object")
                        continue
                    self._unknown_keys(r, KNOWN_REPLY, where)
                    self._positive_int(r, "review_comment_id", where, required=True)
                    self._file_ref(r, "body_file", where, required=True)
                if replies and pr_number is None:
                    self.err("manifest: replies need pr_number (the PR whose review comments they answer)")

        threads = m.get("resolve_threads")
        if threads is not None:
            if not isinstance(threads, list):
                self.err("manifest: resolve_threads must be a list")
            else:
                for i, t in enumerate(threads):
                    if not isinstance(t, str) or not THREAD_RE.match(t):
                        self.err(f"resolve_threads[{i}]: must match ^PRRT_[A-Za-z0-9_-]+$")
                if threads and pr_number is None:
                    self.err("manifest: resolve_threads need pr_number (the PR the threads belong to)")

        issues = m.get("issues")
        if issues is not None:
            if not isinstance(issues, list):
                self.err("manifest: issues must be a list")
            else:
                for i, it in enumerate(issues):
                    where = f"issues[{i}]"
                    if not isinstance(it, dict):
                        self.err(f"{where}: must be an object")
                        continue
                    self._unknown_keys(it, KNOWN_ISSUE, where)
                    irepo = self._str(it, "repo", where, required=True)
                    if irepo is not None:
                        if not REPO_RE.match(irepo):
                            self.err(f"{where}: repo {irepo!r} is not owner/name")
                        elif irepo.lower() not in self.allowed_issue_repos:
                            self.err(f"{where}: repo {irepo!r} is not in the allowed issue repos")
                    self._str(it, "title", where, required=True, max_len=MAX_TITLE_CHARS)
                    self._file_ref(it, "body_file", where, required=True)
                    self._labels(it, where)
                    self._positive_int(it, "comment_on", where, required=False)

        stage = m.get("stage")
        if stage is not None and stage not in STAGES:
            self.err(f"manifest: stage must be one of {', '.join(STAGES)}")

        self._bool(m, "handback", "manifest", required=False)
        if m.get("handback") is True and pr_number is None and not (pr and pr.get("open") is True):
            self.err("manifest: handback needs a PR (pr_number, or pr.open)")

        self._file_ref(m, "handoff_body_file", "manifest", required=False)
        self._file_ref(m, "provenance_comment_file", "manifest", required=False)

        verdict = self._str(m, "review_verdict", "manifest", required=False)
        if verdict is not None:
            if verdict not in VERDICTS:
                self.err(f"manifest: review_verdict must be one of {', '.join(VERDICTS)}")
            if pr_number is None:
                self.err("manifest: review_verdict needs pr_number (the PR the verdict is for)")

        error = m.get("error")
        if error is not None:
            if not isinstance(error, dict):
                self.err("manifest: error must be an object")
            else:
                self._unknown_keys(error, KNOWN_ERROR, "error")
                self._str(error, "message", "error", required=True, max_len=MAX_MESSAGE_CHARS)
                self._bool(error, "fail_run", "error", required=True)

        return self.errors


def validate(manifest, **kwargs) -> list[str]:
    """Return the list of violations (empty when the manifest is valid)."""
    return Validator(manifest, **kwargs).run()


def load_manifest(artifact_dir: Path) -> tuple[object, list[str]]:
    path = Path(artifact_dir) / "manifest.json"
    if path.is_symlink():
        return None, ["manifest.json is a symlink"]
    if not path.is_file():
        return None, ["manifest.json is missing"]
    size = path.stat().st_size
    if size > MAX_MANIFEST_BYTES:
        return None, [f"manifest.json is {size} bytes; the cap is {MAX_MANIFEST_BYTES}"]
    try:
        with path.open("rb") as fh:
            return json.loads(fh.read().decode("utf-8")), []
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"manifest.json is not valid UTF-8 JSON: {exc}"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", required=True, help="artifact directory holding manifest.json")
    ap.add_argument("--repo", required=True, help="owner/name the land job operates on")
    ap.add_argument("--run-id", required=True, help="$GITHUB_RUN_ID")
    ap.add_argument("--default-branch", required=True, help="the repo's default branch (empty refuses the manifest)")
    ap.add_argument(
        "--refused-branches",
        default="",
        help="comma-separated branch names refused as `branch` on top of the default branch (the land job passes `main`)",
    )
    ap.add_argument(
        "--allowed-issue-repos",
        default="",
        help="comma-separated owner/name list issues[] may target",
    )
    ap.add_argument(
        "--pr-head-ref",
        default="",
        help="headRefName of manifest.pr_number as read from the API (required when pr_number is set)",
    )
    ap.add_argument(
        "--event-pr-number",
        default="",
        help="the PR the run's event names (trusted); manifest.pr_number must equal it, and be null when it is empty",
    )
    ap.add_argument(
        "--event-issue-number",
        default="",
        help="the issue the run's event names (trusted); manifest.issue_number must equal it, and be null when it is empty",
    )
    ap.add_argument(
        "--branch-prefix",
        default="",
        help="when --event-pr-number is empty, manifest.branch must start with this (e.g. claude/issue-N-); empty leaves the branch agent-chosen on such runs",
    )
    ap.add_argument(
        "--refuse-bundle",
        action="store_true",
        help="refuse a manifest that carries commits (the caller's agent never commits — the reviewer); has_bundle must be false, head_sha must equal start_sha and no commits.bundle may be present",
    )
    args = ap.parse_args(argv)

    manifest, errors = load_manifest(Path(args.dir))
    if not errors:
        errors = validate(
            manifest,
            artifact_dir=Path(args.dir),
            repo=args.repo,
            run_id=args.run_id,
            default_branch=args.default_branch,
            allowed_issue_repos=args.allowed_issue_repos.split(","),
            pr_head_ref=args.pr_head_ref,
            refused_branches=args.refused_branches.split(","),
            event_pr_number=args.event_pr_number,
            event_issue_number=args.event_issue_number,
            branch_prefix=args.branch_prefix,
            refuse_bundle=args.refuse_bundle,
        )
    for line in errors:
        print(f"manifest violation: {line}")
    if errors:
        print(f"{len(errors)} violation(s); refusing the manifest.")
        return 1
    print("manifest ok.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
