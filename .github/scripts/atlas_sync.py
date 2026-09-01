#!/usr/bin/env python3
"""Hourly Atlas sync — external-review discovery + upstream state sync.

Two tasks (design: meridianlabs-ai/agents design/atlas-tracking.md → "The
hourly Atlas sync"):

1. Discovery: open upstream PRs review-requested-to / assigned-to REVIEWER,
   authored by external community contributors (not org members, not bots),
   not already tracked -> create a proxy issue in the fork (External label,
   Stage=Review, Upstream PR field), then post @review on it so the external
   review is already running before a human opens the proxy.
2. State sync: every fork issue on Atlas with a non-empty "Upstream PR"
   field — OPEN ones, plus CLOSED ones whose Stage is still set (the
   Development-panel auto-close signature; such an issue is reopened while
   its upstream PR is open, unless a human closed it by hand — the
   ClosedEvent's closer discriminates) -> read the upstream PR and advance
   the stage:
     - External proxies: merged/closed -> close proxy (Done); APPROVED ->
       Merge (the merge-approved-prs queue; dismiss the approval or request
       changes to pull one back, sticky APPROVED re-queues a moved card);
       in Merge without a standing approval -> Review; while in Contributor,
       contributor activity newer than the reviewer's last activity (or a
       re-review request) -> Review.
     - Promotions: APPROVED -> Merge (gated on any ts-mono companion
       being merged or approved); CHANGES_REQUESTED -> Review;
       approval dismissed -> Merge->Sign-off only; reviewer
       comment/question while at Sign-off (no verdict) -> Review;
       merged -> close (Done); closed unmerged -> Review + comment.

Deterministic; runs as the machine account (GH_TOKEN=MARVIN_TOKEN). Per-item
failures warn and continue. Every write is idempotent except the @review
trigger comment, which is at-most-once PER STATE CHANGE: the create path
posts it for the first review, and the Contributor -> Review transition posts
it for each new contributor round (dedup skips tracked proxies, and heal
never re-requests), so a failure there is
surfaced as a "review request FAILED" action + warning rather than retried —
the manual re-run path is commenting @review on the proxy.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

UPSTREAM = "UKGovernmentBEIS/inspect_ai"
FORK = "meridianlabs-ai/inspect_ai"
TS_MONO = "meridianlabs-ai/ts-mono"
ORG = "meridianlabs-ai"
REVIEWER = os.environ.get("REVIEWER", "ransomr")
MACHINE_ACCOUNT = "i-am-marvin"  # the login this sync (and the loop) writes as

PROJECT_ID = "PVT_kwDOC7YMCM4BU68p"
PROJECT_NUMBER = 1
STAGE_FIELD = "PVTSSF_lADOC7YMCM4BU68pzhYZEwY"
STAGE_OPTIONS = {
    "Agent": "18c9cd89",
    "Review": "d261eb6b",
    "Sign-off": "da6137e6",
    "Merge": "add17478",
    "Contributor": "39c05a50",
}
STATUS_FIELD = "PVTSSF_lADOC7YMCM4BU68pzhKizZM"
STATUS_OPTIONS = {"Todo": "f75ad846", "In progress": "47fc9ee4", "Done": "98236657"}
UPSTREAM_PR_FIELD = "PVTF_lADOC7YMCM4BU68pzhYZp9Q"

# Stages where the ball is upstream — the promotion mapping's whole domain.
TAIL_STAGES = ("Sign-off", "Merge")

actions: list = []  # human-readable log for the job summary
pending_chips: list = []


def gh(*args: str) -> str:
    res = subprocess.run(["gh", *args], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}...: {res.stderr.strip()[:400]}")
    return res.stdout


def gh_json(*args: str):
    return json.loads(gh(*args))


def gql(query: str, **variables):
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        args += ["-F", f"{k}={v}"] if isinstance(v, (int, bool)) else ["-f", f"{k}={v}"]
    out = gh_json(*args)
    if out.get("errors"):
        raise RuntimeError(
            f"graphql: {out['errors'][0].get('message', out['errors'])[:300]}"
        )
    return out["data"]


def is_org_member(login: str) -> bool:
    """Only a real 404 means "not a member".

    Any other failure (rate limit, 5xx, network) raises, so per-item isolation
    retries next run instead of misclassifying a teammate as external.
    """
    res = subprocess.run(
        ["gh", "api", f"orgs/{ORG}/members/{login}"], capture_output=True, text=True
    )
    if res.returncode == 0:
        return True
    if "HTTP 404" in res.stderr:
        return False
    raise RuntimeError(
        f"org membership check failed for {login}: {res.stderr.strip()[:200]}"
    )


def set_single_select(item_id: str, field_id: str, option_id: str) -> None:
    gql(
        """mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){
             updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,
               value:{singleSelectOptionId:$o}}){projectV2Item{id}}}""",
        p=PROJECT_ID,
        i=item_id,
        f=field_id,
        o=option_id,
    )


def set_text(item_id: str, field_id: str, text: str) -> None:
    gql(
        """mutation($p:ID!,$i:ID!,$f:ID!,$t:String!){
             updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,
               value:{text:$t}}){projectV2Item{id}}}""",
        p=PROJECT_ID,
        i=item_id,
        f=field_id,
        t=text,
    )


def clear_field(item_id: str, field_id: str) -> None:
    gql(
        """mutation($p:ID!,$i:ID!,$f:ID!){
             clearProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f})
             {projectV2Item{id}}}""",
        p=PROJECT_ID,
        i=item_id,
        f=field_id,
    )


def set_stage(item_id: str, stage: str, current) -> bool:
    """Idempotent stage write; active stages also nudge Status to In progress."""
    if current == stage:
        return False
    set_single_select(item_id, STAGE_FIELD, STAGE_OPTIONS[stage])
    set_single_select(item_id, STATUS_FIELD, STATUS_OPTIONS["In progress"])
    return True


# ---------------------------------------------------------------- discovery


def tracked_proxies() -> dict:
    """Upstream URL -> proxy info for every External proxy, open OR closed.

    Closed ones are included to keep a Done proxy from being recreated.
    """
    out = {}
    issues = gh_json(
        "api",
        f"repos/{FORK}/issues?labels=External&state=all&per_page=100",
        "--paginate",
    )
    for i in issues:
        m = re.search(r"https://github\.com/\S+/pull/\d+", i.get("body") or "")
        if m:
            out[m.group(0)] = {
                "number": i["number"],
                "node_id": i["node_id"],
                "open": i["state"] == "open",
            }
    return out


def ensure_on_board(node_id: str, url: str):
    """Idempotently make sure a proxy is on Atlas with its field and a stage.

    Heals a previous run that created the issue but died before the board
    writes (the URL is already in the body, so dedup alone would skip it
    forever and state sync would never see it).
    """
    item = gql(
        """mutation($p:ID!,$c:ID!){addProjectV2ItemById(input:{projectId:$p,contentId:$c}){item{id}}}""",
        p=PROJECT_ID,
        c=node_id,
    )["addProjectV2ItemById"]["item"]["id"]
    cur = gql(
        """query($i:ID!){ node(id:$i){ ... on ProjectV2Item {
             stage: fieldValueByName(name:"Stage"){
               ... on ProjectV2ItemFieldSingleSelectValue{name}}
             up: fieldValueByName(name:"Upstream PR"){
               ... on ProjectV2ItemFieldTextValue{text}} }}}""",
        i=item,
    )["node"]
    healed = False
    if not ((cur.get("up") or {}).get("text") or "").strip():
        set_text(item, UPSTREAM_PR_FIELD, url)
        healed = True
    if not (cur.get("stage") or {}).get("name"):
        set_stage(item, "Review", None)
        healed = True
    return healed


def candidate_prs():
    seen, out = set(), []
    for q in (f"review-requested:{REVIEWER}", f"assignee:{REVIEWER}"):
        res = gh_json(
            "api",
            f"search/issues?q=repo:{UPSTREAM}+type:pr+state:open+{q}&per_page=100",
        )
        for item in res.get("items", []):
            if item["number"] in seen:
                continue
            seen.add(item["number"])
            out.append(item)
    return out


def discover() -> None:
    tracked = tracked_proxies()
    for pr in candidate_prs():
        num, title = pr["number"], pr["title"]
        url = f"https://github.com/{UPSTREAM}/pull/{num}"
        author = pr["user"]["login"]
        try:
            if url in tracked:
                # Heal a partially-seeded OPEN proxy (issue exists, board
                # writes failed); closed proxies stay untouched.
                if tracked[url]["open"] and ensure_on_board(
                    tracked[url]["node_id"], url
                ):
                    actions.append(
                        f"healed proxy #{tracked[url]['number']} for upstream #{num}"
                    )
                continue
            if author.endswith("[bot]") or author == REVIEWER or is_org_member(author):
                continue
            body = (
                f"Tracking review of external contributor PR by {author} in upstream inspect_ai.\n\n"
                f"Upstream PR: {url}\n\n"
                "Labeled `External`; created by the hourly Atlas sync, which also "
                "requests an automated external review below — its findings land on "
                "this issue. Stage starts at **Review**; move it to **Contributor** "
                "after relaying feedback."
            )
            issue = gh_json(
                "api",
                f"repos/{FORK}/issues",
                "-f",
                f"title=Review upstream #{num}: {title}",
                "-f",
                f"body={body}",
                "-f",
                "labels[]=External",
                "-f",
                f"assignees[]={REVIEWER}",
            )
            ensure_on_board(issue["node_id"], url)
            # Kick off the automated external review immediately: marvin's
            # @review comment fires the reviewer's external mode (word-boundary
            # trigger; External label + upstream URL already in the body), so
            # findings are waiting on the proxy by the time a human looks.
            # Isolated try: this is the one write the heal path cannot recover
            # (dedup skips fully-boarded proxies), and a transient failure here
            # must not mislabel the successful creation as a failed item.
            try:
                comment(issue["number"], "@review")
                review_note = "review requested"
            except Exception as e:  # noqa: BLE001
                review_note = "review request FAILED — trigger @review manually"
                print(
                    f"::warning::auto-@review failed on proxy #{issue['number']}: {e}"
                )
            actions.append(
                f"created proxy #{issue['number']} for upstream #{num} ({author}); {review_note}"
            )
            pending_chips.append(f"#{issue['number']} -> {url}")
        except Exception as e:  # noqa: BLE001 — per-item isolation
            print(f"::warning::discovery failed for upstream #{num}: {e}")


# --------------------------------------------------------------- state sync


def board_items():
    """Fork issues on Atlas with a non-empty Upstream PR field.

    Open issues always; CLOSED issues only while their Stage field is still
    set. Every close path the sync owns clears Stage, but the Development-
    panel auto-close (a linked ts-mono companion PR merging; issue #251,
    2026-08-26) does not — so a closed row with Stage set is the zombie
    signature sync_item reopens, while legitimately-terminal items drop out
    of the hourly scan entirely (no upstream query, no skip-log noise).
    Status cannot discriminate: the built-in project workflow moves it to
    Done on both paths.
    """
    after, rows = "", []
    while True:
        data = gql(
            """query($p:ID!,$after:String){ node(id:$p){ ... on ProjectV2 {
                 items(first:100, after:$after){
                   pageInfo{hasNextPage endCursor}
                   nodes{
                     id
                     stage: fieldValueByName(name:"Stage"){
                       ... on ProjectV2ItemFieldSingleSelectValue{name}}
                     up: fieldValueByName(name:"Upstream PR"){
                       ... on ProjectV2ItemFieldTextValue{text}}
                     content{ ... on Issue{
                       number state stateReason repository{nameWithOwner}
                       assignees(first:10){nodes{login}}
                       labels(first:20){nodes{name}} }}}}}}}""",
            p=PROJECT_ID,
            **({"after": after} if after else {}),
        )["node"]["items"]
        for n in data["nodes"]:
            c = n.get("content") or {}
            up = (n.get("up") or {}).get("text", "").strip()
            stage = (n.get("stage") or {}).get("name")
            # CLOSED issues ride along only while Stage is still set — the
            # invisible-zombie signature (see docstring).
            if (
                (c.get("state") == "OPEN" or (c.get("state") == "CLOSED" and stage))
                and c.get("repository", {}).get("nameWithOwner") == FORK
                and up
            ):
                # Pilot scoping: only sync issues assigned to the reviewer —
                # but log the skips: this filter also gates the close-on-merge
                # housekeeping, so a proxy that lost its assignment would
                # otherwise become an invisible zombie.
                if not any(a["login"] == REVIEWER for a in c["assignees"]["nodes"]):
                    actions.append(
                        f"#{c['number']}: skipped (has Upstream PR but not assigned to {REVIEWER})"
                    )
                    continue
                rows.append(
                    {
                        "item": n["id"],
                        "issue": c["number"],
                        "stage": stage,
                        "url": up,
                        "external": any(
                            lbl["name"] == "External" for lbl in c["labels"]["nodes"]
                        ),
                        "open": c.get("state") == "OPEN",
                        "state_reason": c.get("stateReason"),
                    }
                )
        if not data["pageInfo"]["hasNextPage"]:
            return rows
        after = data["pageInfo"]["endCursor"]


def upstream_pr(url: str):
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
    if not m:
        raise ValueError(f"bad upstream URL {url}")
    owner, repo, num = m.group(1), m.group(2), int(m.group(3))
    d = gql(
        """query($o:String!,$r:String!,$n:Int!){ repository(owner:$o,name:$r){
             pullRequest(number:$n){
               state merged mergedAt closedAt reviewDecision headRefName author{login}
               reviewRequests(first:20){nodes{requestedReviewer{... on User{login}}}}
               requestEvents: timelineItems(itemTypes:[REVIEW_REQUESTED_EVENT], last:20){
                 nodes{... on ReviewRequestedEvent{createdAt actor{login}}}}
               changesReviews: reviews(states:[CHANGES_REQUESTED], last:10){
                 nodes{submittedAt}}
             }}}""",
        o=owner,
        r=repo,
        n=num,
    )["repository"]["pullRequest"]
    d["_ref"] = (owner, repo, num)
    # Latest review-request event and latest changes-requested review, as ISO
    # timestamps ("" when absent; ISO compares lexicographically). A *pending*
    # request alone is not a re-request: a second reviewer's never-consumed
    # initial request keeps reviewDecision CHANGES_REQUESTED and the request
    # queue non-empty forever — only a request event NEWER than the latest
    # changes-requested review is the driver handing back.
    d["_req_ts"] = max(
        (n["createdAt"] for n in d["requestEvents"]["nodes"]), default=""
    )
    # Our-side-only variant for the Sign-off ball-possession checks: a
    # request event only counts as "the driver handing the ball upstream"
    # when OUR side performed it — the outside reviewer requesting a
    # colleague's review must not mask their own newer question.
    # (_req_ts stays by-anyone: the Contributor path and `rerequested`
    # deliberately count outside-initiated requests.)
    _ours = {(d.get("author") or {}).get("login", ""), REVIEWER, MACHINE_ACCOUNT}
    d["_our_req_ts"] = max(
        (
            n["createdAt"]
            for n in d["requestEvents"]["nodes"]
            if (n.get("actor") or {}).get("login") in _ours
        ),
        default="",
    )
    d["_changes_ts"] = max(
        (n["submittedAt"] for n in d["changesReviews"]["nodes"]), default=""
    )
    return d


def _pr_events(owner: str, repo: str, num: int):
    """PR author login + flat (author, ts, review_state) event list for a PR.

    Event sources: issue comments, submitted reviews, and review-thread
    comments. The truncation windows (last:50 / last:20) drop old events,
    with one caveat: reviewThreads is ordered by THREAD creation, so on a
    PR with more than 50 threads a fresh reply inside an old thread is
    missed entirely — accepted as rare for the latest-timestamp
    computations built on this. Author nodes carry login and __typename so
    callers can classify humans vs bots; review_state is the review's
    state (e.g. DISMISSED) for review events, the PARENT review's state
    for thread comments (every inline comment belongs to a review, so a
    dismissed review's nits are excludable along with it), and None for
    issue comments.
    """
    d = gql(
        """query($o:String!,$r:String!,$n:Int!){ repository(owner:$o,name:$r){
             pullRequest(number:$n){
               author{login}
               comments(last:50){nodes{author{login __typename} createdAt}}
               reviews(last:50){nodes{author{login __typename} submittedAt state}}
               reviewThreads(last:50){nodes{comments(last:20){nodes{author{login __typename} createdAt pullRequestReview{state}}}}}
             }}}""",
        o=owner,
        r=repo,
        n=num,
    )["repository"]["pullRequest"]
    pr_author = (d.get("author") or {}).get("login", "")
    events = []
    events += [(c["author"], c["createdAt"], None) for c in d["comments"]["nodes"]]
    events += [
        (r["author"], r["submittedAt"], r.get("state"))
        for r in d["reviews"]["nodes"]
        if r.get("submittedAt")
    ]
    for t in d["reviewThreads"]["nodes"]:
        events += [
            (
                c["author"],
                c["createdAt"],
                (c.get("pullRequestReview") or {}).get("state"),
            )
            for c in t["comments"]["nodes"]
        ]
    return pr_author, events


def latest_activity(owner: str, repo: str, num: int) -> tuple[str, str]:
    """(latest author-activity ts, latest reviewer-activity ts) on the PR."""
    author_ts, reviewer_ts = "", ""
    pr_author, events = _pr_events(owner, repo, num)
    for who, ts, _state in events:
        login = (who or {}).get("login", "")
        if login == pr_author and ts > author_ts:
            author_ts = ts
        if login == REVIEWER and ts > reviewer_ts:
            reviewer_ts = ts
    return author_ts, reviewer_ts


def upstream_reviewer_activity(owner: str, repo: str, num: int) -> tuple[str, str]:
    """(latest outside-human ts, latest our-side ts) on a promotion PR.

    Our side is the PR author, REVIEWER, and the machine account; outside
    is any other human. Non-User actors are excluded entirely — CI chatter
    must not yank Sign-off items back hourly. The check is on __typename,
    not a "[bot]" login suffix: that suffix is a REST-API rendering, and
    GraphQL bot logins carry no suffix (e.g. github-actions). DISMISSED
    reviews are excluded too: a dismissed approval keeps its original
    submittedAt, and counting it would move a freshly-dismissed item past
    its documented Sign-off resting place — dismissal (manual or the
    stale-review auto-dismiss on push) retracts the verdict, it is not a
    reviewer response. That exclusion covers the review's inline thread
    comments as well (they carry the parent review's state): an approval
    submitted WITH nits is a common shape, and its nits are part of the
    retracted review — while a genuine thread reply after dismissal
    belongs to a different, non-dismissed review and still counts.
    """
    ours = ""
    others = ""
    pr_author, events = _pr_events(owner, repo, num)
    for who, ts, state in events:
        who = who or {}
        login = who.get("login", "")
        if not login or who.get("__typename") != "User" or state == "DISMISSED":
            continue
        if login in (pr_author, REVIEWER, MACHINE_ACCOUNT):
            if ts > ours:
                ours = ts
        elif ts > others:
            others = ts
    return others, ours


def outside_holds_ball(issue: int, pr) -> bool | None:
    """Whether the newest human word on a promotion PR is an outside one.

    The single ball-possession predicate: outside-human activity newer than
    BOTH our side's latest textual activity and our side's latest review
    request. The request leg (_our_req_ts) covers the driver handing back
    with commits + a re-request but no comment — the reviewer's
    changes-requested review is then the newest textual event, but the ball
    is upstream; an outside actor's request deliberately does not mask
    (the reviewer requesting a colleague must not hide their own newer
    question). Both the Review -> Sign-off promotion gate and the
    Sign-off -> Review yank call this, so the two transitions agree on
    possession by construction and cannot flip an item between them.
    Returns None when the activity lookup fails — callers hold the current
    stage and retry next run (best-effort, warn-logged here).
    """
    owner, repo, num = pr["_ref"]
    try:
        others_ts, ours_ts = upstream_reviewer_activity(owner, repo, num)
    except Exception as e:  # noqa: BLE001 — detection is best-effort
        print(f"::warning::reviewer-activity check failed for #{issue}: {e}")
        return None
    return bool(others_ts and others_ts > max(ours_ts, pr["_our_req_ts"]))


def close_issue(number: int, comment: str) -> None:
    gh("api", f"repos/{FORK}/issues/{number}/comments", "-f", f"body={comment}")
    gh("api", "-X", "PATCH", f"repos/{FORK}/issues/{number}", "-f", "state=closed")


def auto_closed_by_pr(issue: int):
    """Whether the issue's last close was recorded with a closer (PR/commit).

    The Development-panel auto-close (a linked PR merging) records a
    `closer` on the ClosedEvent; a human pressing the close button leaves
    closer null — regardless of stateReason, since the default button
    records COMPLETED, identical to the auto-close signature (issue #204
    was reopened over a deliberate superseded-close, 2026-08-27). Returns
    True/False, or None when the timeline can't be read.
    """
    owner, name = FORK.split("/")
    try:
        nodes = gql(
            """query($o:String!,$r:String!,$n:Int!){ repository(owner:$o,name:$r){
                 issue(number:$n){ timelineItems(itemTypes:[CLOSED_EVENT], last:1){
                   nodes{ ... on ClosedEvent{ closer{__typename} }}}}}}""",
            o=owner,
            r=name,
            n=issue,
        )["repository"]["issue"]["timelineItems"]["nodes"]
    except Exception as e:  # noqa: BLE001 — caller falls back to reopening
        print(f"::warning::closed-event lookup failed for #{issue}: {e}")
        return None
    if not nodes:
        return None
    return nodes[0].get("closer") is not None


def comment(number: int, body: str) -> None:
    gh("api", f"repos/{FORK}/issues/{number}/comments", "-f", f"body={body}")


def companion_pr(issue: int, head_ref: str):
    """The ts-mono companion PR folded into this issue's stage, or None.

    Join key (design: agents design/atlas-tracking.md -> "Multi-repo
    issues"): an explicit `Companion PR: <url>` line in the anchor issue
    body wins; otherwise the branch-name convention — the ts-mono PR whose
    head equals the upstream PR's headRefName (the dev agent names
    companion branches identically in both repos).
    """
    body = (
        gh_json("api", f"repos/{FORK}/issues/{issue}", "--jq", "{body: .body}")["body"]
        or ""
    )
    # precedence: an explicit URL wins over the `none` opt-out, which wins
    # over the branch-name convention
    m = re.search(
        r"Companion PR:\s*(https://github\.com/[^/\s]+/[^/\s]+/pull/\d+)",
        body,
        re.I,
    )
    if not m and re.search(r"Companion PR:\s*none\b", body, re.I):
        return None  # explicit opt-out: no companion, convention disabled
    if m:
        cm = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", m.group(1))
        owner, repo, num = cm.group(1), cm.group(2), int(cm.group(3))
        d = gql(
            """query($o:String!,$r:String!,$n:Int!){ repository(owner:$o,name:$r){
                 pullRequest(number:$n){number state merged reviewDecision
                   latestOpinionatedReviews(first:10){nodes{state}}}}}""",
            o=owner,
            r=repo,
            n=num,
        )["repository"]["pullRequest"]
        d["_repo"] = f"{owner}/{repo}"
        return d
    if not head_ref:
        return None
    owner, repo = TS_MONO.split("/")
    nodes = gql(
        """query($o:String!,$r:String!,$h:String!){ repository(owner:$o,name:$r){
             pullRequests(headRefName:$h, first:5, orderBy:{field:UPDATED_AT,direction:DESC}){
               nodes{number state merged reviewDecision
                 latestOpinionatedReviews(first:10){nodes{state}}}}}}""",
        o=owner,
        r=repo,
        h=head_ref,
    )["repository"]["pullRequests"]["nodes"]
    # prefer an open PR; else the most recently updated (merged counts)
    pick = next((n for n in nodes if n["state"] == "OPEN"), nodes[0] if nodes else None)
    if pick:
        pick["_repo"] = TS_MONO
    return pick


def companion_approved(comp) -> bool:
    """Approval that works without required-review branch protection.

    `reviewDecision` is null on repos without a required-review rule
    (ts-mono), even with APPROVED reviews standing — so fall back to the
    per-reviewer latest opinionated reviews. A standing
    CHANGES_REQUESTED (either surface) blocks: the fallback must not be
    LOOSER than the decision it substitutes for.
    """
    decision = comp.get("reviewDecision")
    if decision is not None:
        # any non-null decision is authoritative: REVIEW_REQUIRED means the
        # repo's rule (approval count, CODEOWNERS, writer-only) is UNMET even
        # if some approval stands — the fallback is only for repos with no
        # rule at all, where the decision is null.
        return decision == "APPROVED"
    states = [
        r.get("state")
        for r in (comp.get("latestOpinionatedReviews") or {}).get("nodes") or []
    ]
    return "APPROVED" in states and "CHANGES_REQUESTED" not in states


def companion_blocks_merge(issue: int, pr) -> bool:
    """True when an existing companion is open and unreviewed.

    The merge queue can merge an OPEN companion (it sequences ts-mono
    first), but a substantive viewer change should pass ts-mono's own
    review before queueing — merged or APPROVED companions pass. No
    companion at all passes trivially.
    """
    comp = companion_pr(issue, pr.get("headRefName") or "")
    if comp is None or comp["merged"] or companion_approved(comp):
        return False
    if comp["state"] == "CLOSED":  # closed unmerged: not a blocker, but note it
        actions.append(
            f"#{issue}: note — companion {comp['_repo']}#{comp['number']} closed unmerged"
        )
        return False
    actions.append(
        f"#{issue}: upstream approved but waiting on companion "
        f"{comp['_repo']}#{comp['number']} (open, unreviewed) — holding stage"
    )
    return True


def reopen_marker(url: str) -> str:
    """Shared prefix of the sync's own recovery-reopen comments.

    Load-bearing: field_is_stale keys on this prefix to tell the sync's
    own reopen (which exists to decide THIS PR's fate) from a reopen that
    starts a new generation of work — change it and the closed-early
    recovery path starts retiring its own field an hour later.
    """
    return f"Reopened — upstream PR {url} "


def field_is_stale(issue: int, pr, url: str) -> bool:
    """True when the issue was reopened AFTER its upstream PR went terminal.

    The Upstream PR field is generational: it describes ONE promotion. Once
    that PR merges (or dies) and a human/automation reopens the issue, new
    work is in flight that the field no longer represents — and without
    this check the hourly scan re-closes the reopened issue forever
    (observed: #232, re-closed on 2026-08-18 and again on 2026-09-01 over
    an in-flight @auto branch, because the field still pointed at a PR
    merged two weeks prior). Detection is the last ReopenedEvent vs the
    PR's terminal timestamp — EXCEPT when that reopen is the sync's own
    closed-early recovery: it postdates the terminal by construction but
    exists to decide this same PR's fate, not to start a new generation.
    Its marker comment (see reopen_marker) lands right after the
    ReopenedEvent, so a marker at/after the last reopen means the reopen
    was the sync's own. Best-effort: unreadable timeline -> not stale
    (the old behavior).
    """
    terminal_ts = pr.get("mergedAt") or pr.get("closedAt") or ""
    if not terminal_ts:
        return False
    try:
        d = gql(
            """query($o:String!,$r:String!,$n:Int!){ repository(owner:$o,name:$r){
                 issue(number:$n){
                   timelineItems(itemTypes:[REOPENED_EVENT], last:1){
                     nodes{ ... on ReopenedEvent{ createdAt }}}
                   comments(last:100){nodes{ body createdAt }}}}}""",
            o=FORK.split("/")[0],
            r=FORK.split("/")[1],
            n=issue,
        )["repository"]["issue"]
    except Exception as e:  # noqa: BLE001
        print(f"::warning::reopen-timeline check failed for #{issue}: {e}")
        return False
    nodes = d["timelineItems"]["nodes"]
    reopened_ts = nodes[0]["createdAt"] if nodes else ""
    if not (reopened_ts and reopened_ts > terminal_ts):
        return False
    marker = reopen_marker(url)
    return not any(
        c["body"].startswith(marker) and c["createdAt"] >= reopened_ts
        for c in d["comments"]["nodes"]
    )


def retire_stale_field(issue: int, item, url: str) -> None:
    """Clear the generational Upstream PR field and say so on the issue.

    Comment first, then clear — the trade close_issue already makes: a
    failure between the two costs a duplicate comment on the retry, not a
    silent retire (a cleared field drops the row from the scan, so nothing
    would ever retry the comment).
    """
    comment(
        issue,
        f"Upstream PR field cleared: {url} reached its terminal state before "
        "this issue was reopened, so it no longer represents the work here — "
        "the next promotion will set the field afresh. (Atlas sync)",
    )
    clear_field(item, UPSTREAM_PR_FIELD)
    actions.append(f"#{issue}: reopened after upstream terminal -> field cleared")


def companion_leftover_warning(issue: int, pr) -> None:
    """Warn about a companion left open after the upstream merge.

    CI forces the companion onto ts-mono main before upstream can merge,
    so an OPEN companion here is a leftover to close by hand — warn only.
    """
    comp = companion_pr(issue, pr.get("headRefName") or "")
    if comp is not None and comp["state"] == "OPEN":
        actions.append(
            f"#{issue}: WARNING companion {comp['_repo']}#{comp['number']} "
            f"still open after upstream merge — close it manually"
        )


def sync_item(row) -> None:
    pr = upstream_pr(row["url"])
    owner, repo, num = pr["_ref"]
    stage, item, issue = row["stage"], row["item"], row["issue"]

    if not row["open"]:
        # A closed issue whose upstream PR is still open was closed by
        # something other than the sync — in practice a Development-panel-
        # linked ts-mono companion PR merging (GitHub treats every panel-
        # linked PR as a closer). Reopen it; the panel link stays (it is the
        # board's PR pill).
        if (
            field_is_stale(issue, pr, row["url"])
            and row["state_reason"] not in ("NOT_PLANNED", "DUPLICATE")
            and auto_closed_by_pr(issue) is not False
        ):
            # The close ratified a stale field: the issue was reopened for
            # new work after this PR went terminal, then closed again — in
            # practice a NEW-generation companion panel-closing it. The
            # terminal housekeeping below would mark it Done off the old
            # generation's field and strand the in-flight work, so reopen
            # and retire the field instead. The deliberate-close guards
            # mirror the checks below (and a closer-lookup failure prefers
            # the reopen, same as the fall-through below).
            gh("api", "-X", "PATCH", f"repos/{FORK}/issues/{issue}", "-f", "state=open")
            retire_stale_field(issue, item, row["url"])
            set_single_select(item, STATUS_FIELD, STATUS_OPTIONS["In progress"])
            actions.append(f"#{issue}: closed off a stale upstream field -> reopened")
            return
        if pr["merged"] or (pr["state"] == "CLOSED" and row["external"]):
            # Terminal within the sync gap: the companion merge closed the
            # issue, then the upstream PR went terminal before this run — the
            # normal close paths never saw it, so do their board housekeeping
            # here. Clearing Stage also drops the row from future scans.
            # (Closed-unmerged counts as terminal only for external proxies,
            # whose open path closes to Done too; a non-external issue whose
            # upstream was declined is reopened into Review below instead —
            # it needs a human decision, not a silent Done.)
            clear_field(item, STAGE_FIELD)
            set_single_select(item, STATUS_FIELD, STATUS_OPTIONS["Done"])
            actions.append(
                f"#{issue}: closed with upstream terminal -> Stage cleared (Done)"
            )
            return
        if row["state_reason"] in ("NOT_PLANNED", "DUPLICATE"):
            # A deliberate human close (the panel auto-close records
            # COMPLETED) — respect it. Clearing Stage takes the item out of
            # the hourly scan; reopening the issue by hand restores sync.
            clear_field(item, STAGE_FIELD)
            actions.append(
                f"#{issue}: closed as {row['state_reason'].lower().replace('_', ' ')} "
                "with upstream PR unmerged -> left closed, Stage cleared"
            )
            return
        if auto_closed_by_pr(issue) is False:
            # No closer on the ClosedEvent: a human closed it by hand, just
            # with the default button (COMPLETED — same stateReason the panel
            # auto-close records, so the check above can't catch it). Respect
            # the close like a not-planned one. On a lookup failure (None)
            # fall through to reopening instead: a wrong reopen has the
            # escape-hatch comment below, a wrong leave-closed strands
            # silently.
            clear_field(item, STAGE_FIELD)
            actions.append(
                f"#{issue}: closed by hand with upstream PR unmerged "
                "-> left closed, Stage cleared"
            )
            return
        gh("api", "-X", "PATCH", f"repos/{FORK}/issues/{issue}", "-f", "state=open")
        escape_hatch = (
            "If the close was deliberate, close it again (any reason), or "
            "clear the item's Upstream PR field — the sync respects both. "
            "(Atlas sync)"
        )
        if pr["state"] == "CLOSED":
            # Non-external with upstream closed unmerged: the same
            # human-decision case the open path routes to Review below — the
            # companion already landed on ts-mono main but the Python never
            # will. Runs at most once per issue: next hour the reopened row
            # takes the open path, whose TAIL_STAGES gate keeps Review
            # parked — and whose staleness gate stays quiet, because
            # field_is_stale recognizes this reopen's marker comment and
            # knows the field still names the PR under decision.
            comment(
                issue,
                reopen_marker(row["url"]) + "was closed unmerged and "
                "needs a human decision; this issue was closed early (most "
                f"likely by a linked companion PR merging). {escape_hatch}",
            )
            if not set_stage(item, "Review", stage):
                # already at Review: set_stage no-oped, but the panel
                # auto-close forced Status to Done — restore it directly
                set_single_select(item, STATUS_FIELD, STATUS_OPTIONS["In progress"])
            actions.append(
                f"#{issue}: closed with upstream closed unmerged -> reopened (Review)"
            )
            return
        comment(
            issue,
            reopen_marker(row["url"]) + "is still open; this issue "
            "was closed early (most likely by a linked companion PR merging). "
            f"{escape_hatch}",
        )
        set_single_select(item, STATUS_FIELD, STATUS_OPTIONS["In progress"])
        actions.append(f"#{issue}: closed while upstream PR open -> reopened")
        # fall through to normal stage sync on the now-open item

    if pr["merged"]:
        if field_is_stale(issue, pr, row["url"]):
            retire_stale_field(issue, item, row["url"])
            return
        try:
            companion_leftover_warning(issue, pr)
        except Exception as e:  # noqa: BLE001 — warn-only, never blocks Done
            print(f"::warning::companion leftover check failed for #{issue}: {e}")
        close_issue(
            issue, f"Upstream PR {row['url']} was merged — closing. (Atlas sync)"
        )
        clear_field(item, STAGE_FIELD)
        set_single_select(item, STATUS_FIELD, STATUS_OPTIONS["Done"])
        actions.append(f"#{issue}: upstream merged -> closed (Done)")
        return

    if pr["state"] == "CLOSED":  # closed without merge
        if field_is_stale(issue, pr, row["url"]):
            retire_stale_field(issue, item, row["url"])
            return
        if row["external"]:
            close_issue(
                issue,
                f"Upstream PR {row['url']} was closed unmerged — closing. (Atlas sync)",
            )
            clear_field(item, STAGE_FIELD)
            set_single_select(item, STATUS_FIELD, STATUS_OPTIONS["Done"])
            actions.append(f"#{issue}: upstream closed -> proxy closed")
        elif stage in TAIL_STAGES and set_stage(item, "Review", stage):
            # Gated to the tail so a stage the driver parked elsewhere is left
            # alone — and the comment can't be re-posted on an hourly bounce.
            comment(
                issue,
                f"Upstream PR {row['url']} was closed unmerged — needs a human decision. (Atlas sync)",
            )
            actions.append(f"#{issue}: upstream closed unmerged -> Review")
        return

    # True re-request: pending request(s) AND the latest request event is newer
    # than the latest changes-requested review (see upstream_pr). A stale
    # never-consumed request from a second reviewer does NOT count — treating
    # it as one would yank human-parked items hourly.
    pending_request = any(
        (n.get("requestedReviewer") or {}).get("login")
        for n in pr["reviewRequests"]["nodes"]
    )
    rerequested = (
        pending_request
        and pr["_req_ts"] > pr["_changes_ts"]
        and pr["_changes_ts"] != ""
    )
    decision = pr.get("reviewDecision")

    if row["external"]:
        if decision == "APPROVED":
            # Maintainer approval hands the external PR to the merge queue
            # (the merge-approved-prs skill drives Merge-stage items home).
            # APPROVED is sticky, so this holds the proxy in Merge each hour —
            # deliberate: to pull a queued external back, dismiss the approval
            # or request changes upstream, not just move the card.
            if stage != "Merge" and companion_blocks_merge(issue, pr):
                return
            if set_stage(item, "Merge", stage):
                actions.append(f"#{issue}: upstream approved -> Merge")
            return
        if stage == "Merge":
            # Was queued but the approval no longer stands (dismissed, or a
            # changes-requested round started) — back to Review: the ball is
            # with the maintainer to re-decide, not with the contributor.
            if set_stage(item, "Review", stage):
                actions.append(f"#{issue}: approval no longer stands -> Review")
            return
        if stage == "Contributor":
            author_ts, reviewer_ts = latest_activity(owner, repo, num)
            # Same staleness rule, against MY last activity: only a request to
            # me newer than my last review/comment pulls the proxy back — my
            # own never-consumed initial request must not.
            rerequested_to_me = (
                any(
                    (n.get("requestedReviewer") or {}).get("login") == REVIEWER
                    for n in pr["reviewRequests"]["nodes"]
                )
                and pr["_req_ts"] > reviewer_ts
            )
            if (author_ts and author_ts > reviewer_ts) or rerequested_to_me:
                if set_stage(item, "Review", stage):
                    actions.append(f"#{issue}: contributor responded -> Review")
                    # The stage move alone leaves the reviewer ignorant: the
                    # creation-path @review is at-most-once, so without this
                    # the new contributor round never gets an automated
                    # review (observed: #360, 2026-08-31). Posting inside the
                    # set_stage(...) success branch keeps the same
                    # once-per-state-change property — the transition fires
                    # once per contributor round, not hourly. Best-effort,
                    # like the creation path: a failure is surfaced, and the
                    # manual fallback is commenting the trigger by hand.
                    try:
                        comment(
                            issue,
                            "@review — the contributor has responded on the "
                            "upstream PR since the last review; re-review "
                            "the current head. (Atlas sync)",
                        )
                        actions.append(f"#{issue}: re-review requested")
                    except Exception as e:  # noqa: BLE001
                        print(
                            f"::warning::re-review request failed on proxy #{issue}: {e}"
                        )
                        actions.append(
                            f"#{issue}: re-review request FAILED — trigger @review manually"
                        )
        return

    # The one transition allowed OUT of Review: the driver re-requested
    # review upstream after a changes-requested round — the fork's analog of
    # "you send it to a second reviewer", i.e. Review -> Sign-off. The
    # signal is precise (sticky CHANGES_REQUESTED + a pending re-request), so
    # a fresh promotion parked in Review (decision REVIEW_REQUIRED)
    # stays parked.
    if stage == "Review" and decision == "CHANGES_REQUESTED" and rerequested:
        # ...unless the reviewer has already responded since the re-request.
        # A plain comment or inline question consumes neither the pending
        # request nor the sticky decision, so this condition would otherwise
        # stay true while the Sign-off branch below yanks the item back —
        # the two transitions flipping it every run. outside_holds_ball is
        # the yank's predicate too, so both agree on possession; on lookup
        # failure (None) hold the stage — the promotion just waits a run.
        held = outside_holds_ball(issue, pr)
        if held is None:
            return
        if held:
            actions.append(
                f"#{issue}: re-requested but the reviewer responded since — holding Review"
            )
            return
        if set_stage(item, "Sign-off", stage):
            actions.append(f"#{issue}: review re-requested upstream -> Sign-off")
        return

    # Promotion tail — only while the ball is genuinely upstream. Stages a
    # human parked elsewhere (Agent, Review, ...) are out of this
    # mapping's domain: reviewDecision is sticky (CHANGES_REQUESTED persists
    # until a re-review), so acting from any stage would drag parked items back
    # here every hour.
    if stage not in TAIL_STAGES:
        return
    if decision == "CHANGES_REQUESTED" and rerequested:
        # Stale: fixes were pushed and a re-review requested; the recorded
        # decision persists until the reviewer acts. Treat as review-pending.
        decision = None
    if decision == "APPROVED":
        if stage != "Merge" and companion_blocks_merge(issue, pr):
            return
        if set_stage(item, "Merge", stage):
            actions.append(f"#{issue}: upstream approved -> Merge")
    elif decision == "CHANGES_REQUESTED":
        if set_stage(item, "Review", stage):
            actions.append(f"#{issue}: upstream changes requested -> Review")
    else:  # REVIEW_REQUIRED / None
        if stage == "Merge" and set_stage(item, "Sign-off", stage):
            actions.append(f"#{issue}: approval dismissed -> Sign-off")
            return
        if stage == "Sign-off":
            # A reviewer response that is neither approval nor a formal
            # changes-request — a question, a comment-only review, an inline
            # thread — still puts the ball back with us; parked at Sign-off
            # it sat invisible until someone happened to look (observed:
            # upstream #5096, an approver comment awaited unnoticed,
            # 2026-09-01). Responding upstream advances our timestamp, so
            # the hourly scan doesn't re-yank, and the Review -> Sign-off
            # promotion above holds on this same predicate, so the moved
            # item rests until our side responds.
            if outside_holds_ball(issue, pr) and set_stage(item, "Review", stage):
                actions.append(f"#{issue}: upstream reviewer responded -> Review")


def lifecycle_item(issue: int):
    """The Atlas item + current Stage for a fork issue, or None.

    Pilot-scoped like the rest of the sync: only open issues assigned to
    REVIEWER, and only their item on THIS project.
    """
    d = gql(
        """query($o:String!,$r:String!,$n:Int!){ repository(owner:$o,name:$r){
             issue(number:$n){ state assignees(first:10){nodes{login}}
               projectItems(first:10){nodes{id project{id}
                 fieldValueByName(name:"Stage"){
                   ... on ProjectV2ItemFieldSingleSelectValue{name}}}}}}}""",
        o=FORK.split("/")[0],
        r=FORK.split("/")[1],
        n=issue,
    )["repository"]["issue"]
    if d is None or d["state"] != "OPEN":
        return None
    if REVIEWER not in [a["login"] for a in d["assignees"]["nodes"]]:
        return None
    for node in d["projectItems"]["nodes"]:
        if node["project"]["id"] == PROJECT_ID:
            fv = node.get("fieldValueByName") or {}
            return (node["id"], fv.get("name"))
    return None


def reflect_companion_loops() -> None:
    """Reflect companion auto-loops onto their anchors' lifecycle stage.

    A ts-mono PR is the companion (active half) of fork issue N when its
    claude/issue-N-* branch also exists on the fork — the number alone is
    not identity, since ts-mono's own agent mints identically-shaped
    names for ts-mono issues. Engagement then needs BOTH the `auto`
    label (the no-progress escalation exits the loop by removing it,
    posting no marker) and a continue-marker newer than any stop-marker:
    continue = a bare re-review trigger comment or
    `claude-review-verdict:suggestions`; stop = `auto-handoff` /
    `auto-converged` / `claude-review-verdict:clean`. Engaged -> the
    machinery owns the issue (Agent); otherwise a human does (Review).
    Only items already in Agent/Review move — parked stages are left
    alone, same as the promotion tail.
    """
    try:
        prs = gh_json(
            "pr",
            "list",
            "--repo",
            TS_MONO,
            "--state",
            "open",
            # explicit: gh defaults to 30, and silent truncation would freeze
            # any companion outside the newest page — the exact blind spot
            # this function exists to prevent
            "--limit",
            "200",
            "--json",
            "number,headRefName,labels",
        )
    except RuntimeError as e:
        print(f"::warning::companion reflection: pr list failed: {e}")
        return
    for cpr in prs:
        m = re.match(r"claude/issue-(\d+)-", cpr.get("headRefName") or "")
        if not m:
            continue
        # Companion, not ts-mono-native: the SAME branch must exist on the
        # fork (ts-mono's own dev agent mints identically-shaped names whose
        # numbers reference ts-mono issues — a bare number match would drive
        # an unrelated fork issue's stage).
        try:
            gh("api", f"repos/{FORK}/branches/{cpr['headRefName']}")
        except RuntimeError:
            continue
        n = int(m.group(1))
        anchor = lifecycle_item(n)
        if anchor is None:
            continue
        item, stage = anchor
        if stage not in ("Agent", "Review"):
            continue
        try:
            comments = issue_comments(TS_MONO, cpr["number"])
        except (RuntimeError, ValueError):
            continue
        go_ts, stop_ts = "", ""
        for c in comments:
            body, ts = c.get("body") or "", c.get("created_at") or ""
            if (
                "auto-handoff" in body
                or "auto-converged" in body
                or "claude-review-verdict:clean" in body
            ):
                stop_ts = max(stop_ts, ts)
            elif (
                body.strip() == "@" + "review"
                or "claude-review-verdict:suggestions" in body
            ):
                go_ts = max(go_ts, ts)
        # The no-progress escalation posts NO marker — it exits the loop by
        # REMOVING the auto label (observed: ts-mono#557 escalated and the
        # label-filtered enumeration went blind, freezing the anchor at
        # Agent). Label absence therefore means the ball is with a human,
        # regardless of marker recency.
        labeled = any(lb.get("name") == "auto" for lb in cpr.get("labels") or [])
        engaged = labeled and go_ts > stop_ts
        target = "Agent" if engaged else "Review"
        if set_stage(item, target, stage):
            state = "engaged" if engaged else "handed back"
            actions.append(
                f"#{n}: companion {TS_MONO}#{cpr['number']} loop {state} -> {target}"
            )


def issue_comments(repo: str, num: int) -> list:
    """All comments on an issue/PR, pagination-safe.

    `--paginate` emits one JSON document PER PAGE (with or without
    `--jq`), so json.loads breaks past 100 comments; `--slurp` wraps the
    pages in an outer array we flatten here.
    """
    pages = gh_json(
        "api",
        "--paginate",
        "--slurp",
        f"repos/{repo}/issues/{num}/comments?per_page=100",
    )
    return [c for page in pages for c in page]


def pr_has_live_run(title: str) -> bool:
    """True when a queued or in-progress agent run matches this PR's title.

    Verdict consumers post no created-comment ack, and fix rounds
    routinely outlive any fixed threshold — a live run means a consumer
    exists and revival would race it into concurrent duplicate rounds.
    """
    for status in ("in_progress", "queued"):
        n = gh_json(
            "api",
            f"repos/{FORK}/actions/runs?status={status}&per_page=50",
            "--jq",
            "[.workflow_runs[] | .display_title]",
        )
        if any(title and title in t for t in n):
            return True
    return False


def retrigger_stale_handbacks() -> None:
    """Revive auto-loop hand-backs whose consumer run died before starting.

    A hand-back is a comment the loop expects an agent run to consume: a
    re-review trigger (the reviewer runs) or a suggestions verdict (a
    fix round runs). Runner-infra failures at "Set up job" kill the
    consumer before any of its own backstops exist — nothing posts, no
    one is cc'd, the loop waits forever (observed: fork PR #324).
    Detection is reconciler-shaped: on an open auto-labeled PR, if the
    last non-counter comment is a continue signal older than
    STALE_MINUTES (and no standing ⚠️ means a human was already told),
    post a fresh re-review trigger as the machine account.

    Guards against racing a slow-but-healthy consumer: a re-review
    trigger bearing the reviewer's startup ack (eyes reaction) has a
    consumer that genuinely started — its in-job backstops own any
    later failure (the dead-at-setup run never acks) — and a PR with a
    queued or in-progress run matching its title is left alone. The
    revival comment starts with the trigger token, so it is itself a
    continue signal and gets revived again if ITS run also dies —
    infra outages are correlated.
    """
    STALE_MINUTES = 45
    try:
        prs = gh_json(
            "pr",
            "list",
            "--repo",
            FORK,
            "--label",
            "auto",
            "--state",
            "open",
            "--limit",
            "200",
            "--json",
            "number,title",
        )
    except RuntimeError as e:
        print(f"::warning::stale-handback check: pr list failed: {e}")
        return
    now = datetime.now(timezone.utc)
    for pr in prs:
        num = pr["number"]
        try:
            comments = issue_comments(FORK, num)
        except (RuntimeError, ValueError):
            continue
        last = None
        for c in comments:
            body = c.get("body") or ""
            if "auto-fix-attempts" in body or "auto-review-rounds" in body:
                continue  # sticky counters, created mid-round — not signals
            last = c
        if last is None:
            continue
        body = last.get("body") or ""
        is_trigger = body.strip().startswith("@" + "review")
        is_continue = is_trigger or "claude-review-verdict:suggestions" in body
        if not is_continue or "⚠️" in body:
            continue
        ts = datetime.fromisoformat(
            (last.get("created_at") or "").replace("Z", "+00:00")
        )
        age_min = (now - ts).total_seconds() / 60
        if age_min < STALE_MINUTES:
            continue
        if is_trigger and last.get("id"):
            try:
                acked = gh_json(
                    "api",
                    f"repos/{FORK}/issues/comments/{last['id']}/reactions",
                    "--jq",
                    '[.[] | select(.content == "eyes")] | length',
                )
                if int(acked or 0) > 0:
                    continue  # consumer started; in-job backstops own it
            except (RuntimeError, ValueError):
                pass  # unreadable reactions: fall through to the run check
        try:
            if pr_has_live_run(pr.get("title") or ""):
                continue
        except (RuntimeError, ValueError):
            continue  # can't tell — racing a live consumer is worse
        comment(
            num,
            "@" + "review — re-triggered by the Atlas sync: the previous "
            f"hand-back sat unconsumed for {int(age_min)} minutes (its agent "
            "run likely died before starting). (Atlas sync)",
        )
        actions.append(
            f"PR #{num}: stale hand-back ({int(age_min)}m) -> review re-triggered"
        )


def main() -> int:
    discover()
    for row in board_items():
        try:
            sync_item(row)
        except Exception as e:  # noqa: BLE001 — per-item isolation
            print(f"::warning::sync failed for #{row['issue']}: {e}")
    try:
        reflect_companion_loops()
    except Exception as e:  # noqa: BLE001 — reflection must not break the sync
        print(f"::warning::companion reflection failed: {e}")
    try:
        retrigger_stale_handbacks()
    except Exception as e:  # noqa: BLE001 — revival must not break the sync
        print(f"::warning::stale-handback check failed: {e}")

    print("\n=== Atlas sync summary ===")
    for a in actions or ["no changes"]:
        print(f"- {a}")
    if pending_chips:
        print("\nProxies pending a clickable chip (run link-upstream-chips locally):")
        for p in pending_chips:
            print(f"- {p}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Atlas sync\n")
            for a in actions or ["no changes"]:
                f.write(f"- {a}\n")
            if pending_chips:
                f.write("\n**Pending chips** (run `link-upstream-chips` locally):\n")
                for p in pending_chips:
                    f.write(f"- {p}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
