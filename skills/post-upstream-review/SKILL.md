---
name: post-upstream-review
description: Relay an external review upstream — /post-upstream-review <proxy-issue-number> [instructions] takes the review findings from an External proxy issue, posts them as a single review on the contributor's upstream PR (inline comments on the right lines where possible), then moves the proxy to Contributor.
---

# Post external review comments upstream

The relay half of external review tracking (design/atlas-tracking.md): the
automated reviewer posted findings on the **proxy issue**; the maintainer
decides what to relay. This skill composes and posts that feedback on the
contributor's **upstream PR** — as the user (local `gh` identity), never as
marvin or the bot; upstream feedback comes from the maintainer personally.

Arguments: the proxy issue number, plus optional instructions that shape the
relay ("only the blocking one", "soften the tone", "also ask about X").

The proxy is public and the findings quote the contributor's code and text,
so the mechanics are two scripts next to this skill (substitute its base
directory); the rules they follow are in
[the skills' trust model](../THREAT_MODEL.md).

## Steps

1. **Gather.**

   ```sh
   bash <skill-base-dir>/gather.sh <N>
   ```

   It checks that proxy `N` in `meridianlabs-ai/inspect_ai` is labelled
   `External` and was written by a verified author (the machine account, or
   a write-access maintainer, who seeds new proxies by hand), reads the
   upstream PR from its `Upstream PR:` line and confirms it is open, and
   picks the findings comment to relay **by author, not recency**: only a
   comment by the machine account or a write-access collaborator counts
   (Claude Security 4773885). It prints `OK proxy=#N upstream=<url>
   head=<sha> findings=<comment url> by <login> at <time>` and the paths of
   `findings.md` (that comment's body) and `context.json` (for step 4).
   Read the findings from `findings.md`. Exit codes: **3** nothing to relay
   (not labelled External, written by someone else, no upstream PR line, the PR is closed, or no
   trusted findings comment); **4** the newest findings-shaped comment is by
   someone else — stderr names them and the newest trusted one: stop and
   ask the user, never relay it; **5** a review of yours on the upstream PR
   is newer than the findings comment — stop and ask, don't double-relay;
   **2** a GitHub read failed.

2. **Select and rewrite.** Apply the user's instructions (default: relay every
   finding). Rewrite each finding as direct maintainer-to-contributor
   feedback:
   - courteous, concrete, actionable; no internal jargon;
   - NEVER mention the proxy issue, marvin, or Meridian tracking internals —
     but the AI origin IS disclosed, via the standard footer in step 4;
   - label every finding with one of two severities, and keep the label
     through the rewrite (decision: Ransom, 2026-09-24; his two-level
     standard from 2026-09-11):
     - **Blocking**: needs fixing before merge;
     - **Optional**: take it or leave it, and say so in those words.

     No third level: never write "nit", "should fix", "minor" or similar
     as a severity. A finding the review tags non-blocking, nit or minor is
     relayed as Optional;
   - a finding the review lists under "Not this PR" is not relayed as a
     request on the contributor's PR: it is out of scope for their change.

3. **Map to diff lines.** For each finding with a file:line, check the line is
   part of the PR diff (`gh pr diff <M> --repo UKGovernmentBEIS/inspect_ai`,
   `M` from the `OK` line; inline comments can only attach to diff lines,
   RIGHT side for additions). Findings on lines outside the diff go in the
   review body with a `path:line` reference instead.

4. **Post ONE review** (atomic — summary + inline comments together). Write
   it as a JSON file with your file-writing tool, never through a shell
   command or `-f` fields (the text quotes the contributor; rule (a)):

   ```json
   {"body": "<summary>", "event": "REQUEST_CHANGES",
    "comments": [{"path": "src/x.py", "line": 12, "side": "RIGHT", "body": "<finding>"}]}
   ```

   Use `start_line` + `line` for a multi-line comment. `event`:
   `REQUEST_CHANGES` when relaying any blocking finding, else `COMMENT` —
   overridable by the user's instructions. Never `APPROVE` from this skill;
   approval is a separate deliberate act, and the script refuses it. Then:

   ```sh
   bash <skill-base-dir>/post.sh <context.json> <review.json>
   ```

   It runs `gather.sh` again and refuses when anything changed (a newer
   trusted findings comment, an edit to the chosen one, a moved head:
   exit 3), accepts only those
   fields, backticks the agents' trigger phrases, appends the
   AI-generation footer when the body does not end with it:

   ```
   ---
   *This review was AI-generated from findings a maintainer chose to relay; the maintainer did not review its wording.*
   ```

   and refuses (exit 4) a text that GitHub would link to an upstream issue
   or PR other than `M` — the findings were written on the fork, where a
   bare `#N` is a fork issue. Reword the reference (drop the `#`, or name
   what it is); pass `--allow-ref <N>` only for one the user confirmed. It
   prints the review as it will be posted, pins it to the head gather saw,
   posts it with `gh api --input`, then notes the relay on the proxy
   (`Relayed upstream as <review url> (<X> inline) from the findings
   comment <comment url>. Awaiting contributor.`) and moves the proxy's
   stage to **Contributor** (the ball is with them now). `--dry-run` runs
   every check and prints the review without posting. Exit **6**: the
   review is posted but a bookkeeping step failed (stderr says which; do it
   by hand).

5. **Report.** Review URL, the findings comment it relays (by URL), what was
   relayed vs. dropped (and why), inline vs. body placement, stage set. The
   hourly sync brings the proxy back to Human Review when the contributor
   responds — posting this review also updates your last-activity
   timestamp, which is exactly what that detector compares against.

## Cautions

- Outward-facing: everything posted lands on a public PR under the user's
  name — and invoking this skill IS the authorization to post: the maintainer
  reviews the findings on the proxy before invoking, so compose and post
  directly, no preview step (decision: Ransom, 2026-09-30). The script prints
  the review as it posts it; that is a record of the wording, not a chance
  to approve it, and the footer tells the contributor the wording was not
  reviewed by the maintainer.
  Stop and ask when something is genuinely unresolvable: no trusted
  findings comment on the proxy, a newer findings-shaped comment by someone
  else, instructions that contradict each other, a finding that no longer
  matches the PR's current state, or a reference the guard refused that the
  relay needs.
- Do not edit the contributor's PR, push to their branch, or touch labels /
  assignees upstream.
