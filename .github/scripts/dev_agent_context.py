#!/usr/bin/env python3
"""Write the dev agent's context file (the `dev-agent-context` composite).

design/untrusted-agent-job.md → claude.yml: agent mode → Context prompt.
claude.yml's two agent jobs build their prompts from this file. It holds:

- the request that started the run, from the event payload;
- the issue's or pull request's title and body, also from the payload (the
  snapshot at the time of the event, never a live read);
- on an issue run, the issue's comments posted before the run started,
  oldest first, machine control comments dropped, at most the newest 30
  and 40,000 characters, with one line saying how many were left out;
- on a pull-request run, the review summaries submitted before the run
  started, then the PR thread the `pr-feedback-context` composite wrote
  (`--feedback`), which the composite filters by the same time;
- with `--images-dir`, the attachment images those texts link to, each
  downloaded into that directory and named in place of its link.

Everything created or edited at or after `--trigger-time` is left out, so an
author cannot change what the agent reads after a maintainer started the run.

API reads go through `gh api` with the caller's token (GH_TOKEN). A failed
read is retried twice, then fails the step: the context is what the agent
works from. A failed image download is not fatal: the link stays and a
warning is logged. Images are fetched only from GitHub's signed attachment
host, with no redirects, at most 20 of them, 10 MB each and 50 MB in all.
The runner never decodes them; it reads their first bytes to name the file
type the agent's Read tool needs.

Stdlib only.
"""

import argparse
import json
import os
import re
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_HISTORY_COMMENTS = 30
MAX_HISTORY_CHARS = 40_000
MAX_REVIEWS = 10
MAX_REVIEW_CHARS = 20_000
MAX_CONTEXT_BYTES = 100_000
MAX_IMAGES = 20
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_TOTAL = 50 * 1024 * 1024
IMAGE_TIMEOUT_S = 30
FETCH_ATTEMPTS = 3

TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# The machine comments pr-feedback-context drops too: loop markers, the
# provenance note, the dev agent's status comment.
MARKER_RE = re.compile(r"<!-- (auto-review-rounds|auto-fix-attempts|auto-converged|auto-review-head:[^>]*|model-provenance|dev-agent-status) -->")
BARE_TRIGGER_RE = re.compile(r"^\s*@(review|auto|claude)\b(.*)$", re.S)
# An attachment image in Markdown or HTML, as claude-code-action matches it.
GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
ASSET_URL = rf"https://github\.com/user-attachments/assets/{GUID}"
IMAGE_LINK_RE = re.compile(rf"!\[[^\]]*\]\(({ASSET_URL})\)|<img[^>]+src=[\"']({ASSET_URL})[\"']", re.I)
SIGNED_HOST = "private-user-images.githubusercontent.com"
SIGNED_URL_RE = re.compile(r"https://private-user-images\.githubusercontent\.com/[^\"'\s<>]+")
SIGNED_PATH_RE = re.compile(rf"^/[^/]+/[^/]*-({GUID})(?:\.[a-z0-9]+)?$", re.I)
FULL_JSON = "Accept: application/vnd.github.full+json"


def warn(msg):
    print(f"::warning::dev-agent-context: {msg}", file=sys.stderr)


class FetchFailed(Exception):
    pass


def gh_json(args, *, paginate=False):
    """`gh api ARGS` as JSON, retried; a paginated read is slurped into one
    list. Raises FetchFailed after the last attempt."""
    cmd = ["gh", "api", *args] + (["--paginate", "--slurp"] if paginate else [])
    err = ""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if r.returncode == 0:
            try:
                data = json.loads(r.stdout)
            except json.JSONDecodeError as e:
                err = f"unparsable response: {e}"
            else:
                if paginate:
                    return [x for page in data for x in (page if isinstance(page, list) else [page])]
                return data
        else:
            err = r.stderr.strip() or r.stdout.strip()
        if attempt < FETCH_ATTEMPTS:
            time.sleep(attempt * 5)
    raise FetchFailed(f"gh api {' '.join(args)} failed after {FETCH_ATTEMPTS} attempts: {err[:500]}")


def before(t, created, edited=None):
    """Created, and last edited, strictly before the trigger time (all times
    are GitHub's own `YYYY-MM-DDTHH:MM:SSZ`, which compare as strings)."""
    if not t:
        return True
    return bool(created) and created < t and (edited or created) < t


def is_machine_comment(body):
    if MARKER_RE.search(body):
        return True
    if "<!-- claude-review-summary -->" in body and len(re.sub(r"<!--[^>]*-->", "", body)) <= 200:
        return True
    m = BARE_TRIGGER_RE.match(body)
    if m and all(c in string.whitespace or c in string.punctuation for c in m.group(2)):
        return True
    return body.startswith("**Claude finished")


def login(user):
    return (user or {}).get("login") or "?"


def issue_history(repo, number, t):
    """The issue's comments before the trigger, filtered and bounded, as
    (text, raw comment list)."""
    comments = gh_json([f"repos/{repo}/issues/{number}/comments?per_page=100"], paginate=True)
    kept = [c for c in comments
            if before(t, c.get("created_at"), c.get("updated_at")) and not is_machine_comment(c.get("body") or "")]
    kept.sort(key=lambda c: c.get("created_at") or "")
    omitted = max(0, len(kept) - MAX_HISTORY_COMMENTS)
    kept = kept[omitted:]
    entries = [f"--- {login(c.get('user'))} at {c.get('created_at')}:\n{c.get('body') or ''}\n" for c in kept]
    while entries and sum(len(e) for e in entries) > MAX_HISTORY_CHARS:
        entries.pop(0)
        omitted += 1
    lines = ["ISSUE DISCUSSION (the comments posted before this run started, oldest first):"]
    if omitted:
        lines.append(f"({omitted} earlier comment{'s' if omitted != 1 else ''} omitted)")
    if not entries:
        lines.append("(none)")
    return "\n".join(lines + entries) + "\n"


REVIEWS_QUERY = ("query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number)"
                 "{reviews(last:100){nodes{author{login} body state submittedAt lastEditedAt}}}}}")


def pr_reviews(repo, number, t):
    owner, name = repo.split("/", 1)
    data = gh_json(["graphql", "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={number}",
                    "-f", f"query={REVIEWS_QUERY}"])
    nodes = (((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get("reviews", {}).get("nodes") or []
    kept = [r for r in nodes
            if (r.get("body") or "").strip() and r.get("submittedAt")
            and before(t, r.get("submittedAt"), r.get("lastEditedAt"))]
    kept.sort(key=lambda r: r["submittedAt"])
    kept = kept[-MAX_REVIEWS:]
    entries = [f"--- {login(r.get('author'))} ({r.get('state')}) at {r['submittedAt']}:\n{r['body']}\n" for r in kept]
    while entries and sum(len(e) for e in entries) > MAX_REVIEW_CHARS:
        entries.pop(0)
    lines = ["PULL REQUEST REVIEWS (review summaries submitted before this run started, oldest first):"]
    return "\n".join(lines + (entries or ["(none)"])) + "\n"


def request_section(event_name, event, number, is_pr):
    """What started the run, from the payload."""
    kind = "pull request" if is_pr else "issue"
    if event_name in ("issue_comment", "pull_request_review_comment"):
        c = event.get("comment") or {}
        text = f"REQUEST (a comment by {login(c.get('user'))} at {c.get('created_at')} on {kind} #{number}):\n{c.get('body') or ''}\n"
        if event_name == "pull_request_review_comment":
            line = c.get("line") or c.get("original_line") or "?"
            text += (f"\nIt is an inline review comment on {c.get('path') or '?'} line {line}, on this diff hunk:\n"
                     f"{c.get('diff_hunk') or ''}\n")
        return text
    if event_name == "pull_request_review":
        r = event.get("review") or {}
        body = r.get("body") or "(the review has no summary; its inline comments are in REVIEW THREADS below)"
        return (f"REQUEST (a review by {login(r.get('user'))}, {r.get('state')}, at {r.get('submitted_at')} "
                f"on pull request #{number}):\n{body}\n")
    how = {"labeled": f"labelled `{(event.get('label') or {}).get('name')}` by {login(event.get('sender'))}",
           "assigned": f"assigned to {login(event.get('assignee'))} by {login(event.get('sender'))}",
           "opened": f"opened by {login(event.get('sender'))}"}.get(event.get("action"), event.get("action") or "")
    return f"REQUEST: the {kind} below ({how}); its title and body are the task.\n"


def utf8_cap(data: bytes, limit: int) -> bytes:
    if len(data) <= limit:
        return data
    cut = data[:limit]
    return cut.decode("utf-8", "ignore").encode("utf-8")


# --- images ----------------------------------------------------------------


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"redirect to {newurl} refused", headers, fp)


def image_ext(head: bytes):
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:4] == b"GIF8":
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


def signed_urls(repo, number, is_pr):
    """GUID → signed download URL, from the rendered HTML of the bodies the
    context may quote. Live reads: a GUID is used only when it appears in the
    snapshot text, so a later edit can remove an image but never add one."""
    htmls = [(gh_json(["-H", FULL_JSON, f"repos/{repo}/issues/{number}"]) or {}).get("body_html") or ""]
    htmls += [c.get("body_html") or "" for c in gh_json(["-H", FULL_JSON, f"repos/{repo}/issues/{number}/comments?per_page=100"], paginate=True)]
    if is_pr:
        htmls += [c.get("body_html") or "" for c in gh_json(["-H", FULL_JSON, f"repos/{repo}/pulls/{number}/comments?per_page=100"], paginate=True)]
        htmls += [r.get("body_html") or "" for r in gh_json(["-H", FULL_JSON, f"repos/{repo}/pulls/{number}/reviews?per_page=100"], paginate=True)]
    found = {}
    for html in htmls:
        for url in SIGNED_URL_RE.findall(html.replace("&amp;", "&")):
            parsed = urllib.parse.urlsplit(url)
            m = SIGNED_PATH_RE.match(parsed.path)
            if parsed.scheme == "https" and parsed.hostname == SIGNED_HOST and m:
                found.setdefault(m.group(1).lower(), url)
    return found


def download(url, limit, opener):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != SIGNED_HOST or parsed.port not in (None, 443):
        raise ValueError(f"not an attachment URL on {SIGNED_HOST}")
    with opener.open(urllib.request.Request(url, headers={"User-Agent": "dev-agent-context"}), timeout=IMAGE_TIMEOUT_S) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"larger than {limit} bytes")
    return data


def fetch_images(text, repo, number, is_pr, images_dir, opener=None):
    """Replace each attachment image link in TEXT by the file it was saved
    to. Returns the new text and how many were saved."""
    urls = []
    for m in IMAGE_LINK_RE.finditer(text):
        u = m.group(1) or m.group(2)
        if u not in urls:
            urls.append(u)
    if not urls:
        return text, 0
    try:
        signed = signed_urls(repo, number, is_pr)
    except FetchFailed as e:
        warn(f"could not read the rendered bodies, so no image was downloaded; the links stay: {e}")
        return text, 0
    opener = opener or urllib.request.build_opener(NoRedirect)
    os.makedirs(images_dir, mode=0o755, exist_ok=True)
    saved = 0
    total = 0
    for i, url in enumerate(urls):
        if saved >= MAX_IMAGES:
            warn(f"more than {MAX_IMAGES} images; the rest stay links")
            break
        guid = re.search(GUID, url).group(0).lower()
        s = signed.get(guid)
        if not s:
            warn(f"no signed download URL for {url}; the link stays")
            continue
        try:
            data = download(s, min(MAX_IMAGE_BYTES, MAX_IMAGE_TOTAL - total), opener)
        except (OSError, ValueError, urllib.error.URLError) as e:
            warn(f"could not download {url}: {e}; the link stays")
            continue
        ext = image_ext(data[:12])
        if not ext:
            warn(f"{url} is not a PNG, JPEG, GIF or WebP image; the link stays")
            continue
        path = os.path.join(images_dir, f"image-{i + 1}{ext}")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except OSError as e:
            warn(f"could not save {url} as {path}: {e}; the link stays")
            continue
        total += len(data)
        saved += 1
        text = text.replace(url, path)
    return text, saved


# --- main ------------------------------------------------------------------


def build(args, event_name, event):
    entity = event.get("pull_request") or event.get("issue") or {}
    number = entity.get("number")
    if not number:
        raise SystemExit("::error::dev-agent-context: the event names no issue or pull request")
    is_pr = bool(event.get("pull_request")) or bool((event.get("issue") or {}).get("pull_request"))
    t = args.trigger_time
    kind = "PULL REQUEST" if is_pr else "ISSUE"
    parts = [request_section(event_name, event, number, is_pr)]
    parts.append(f"{kind} #{number}: {entity.get('title') or ''}\n{entity.get('body') or '(no description)'}\n")
    if is_pr:
        parts.append(pr_reviews(args.repo, number, t))
        if args.feedback:
            with open(args.feedback, encoding="utf-8") as f:
                parts.append(f.read())
    else:
        parts.append(issue_history(args.repo, number, t))
    text = "\n".join(parts)
    note = ""
    if args.images_dir:
        text, saved = fetch_images(text, args.repo, number, is_pr, args.images_dir)
        if saved:
            note = (f"IMAGES: {saved} image{'s' if saved != 1 else ''} linked below {'were' if saved != 1 else 'was'} "
                    f"downloaded; each link now names its file under {args.images_dir}. Read them with the Read tool.\n\n")
    data = (note + text).encode("utf-8")
    if len(data) > MAX_CONTEXT_BYTES:
        data = utf8_cap(data, MAX_CONTEXT_BYTES - 100) + b"\n(context truncated: it exceeded the size cap)\n"
    return data


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--trigger-time", default="")
    p.add_argument("--feedback", default="")
    p.add_argument("--images-dir", default="")
    args = p.parse_args(argv)
    if args.trigger_time and not TIME_RE.match(args.trigger_time):
        print(f"::error::dev-agent-context: trigger time {args.trigger_time!r} is not of the form 2026-09-30T10:00:00Z")
        return 1
    if not args.trigger_time:
        warn("no trigger time: nothing written after the run started is filtered out")
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
        event = json.load(f)
    try:
        data = build(args, os.environ.get("GITHUB_EVENT_NAME", ""), event)
    except FetchFailed as e:
        print(f"::error::dev-agent-context: {e}")
        return 1
    with open(args.out, "wb") as f:
        f.write(data)
    print(f"dev-agent-context: wrote {len(data)} bytes to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
