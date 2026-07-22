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

## Steps

1. **Gather.** Read proxy issue `N` in `meridianlabs-ai/inspect_ai`: confirm
   the `External` label; get the upstream PR URL (body template line); read
   the latest review-findings comment (`claude[bot]`/machine account). Confirm
   the upstream PR is still OPEN. If the user already posted an upstream
   review NEWER than the findings comment, stop and ask — don't double-relay.

2. **Select and rewrite.** Apply the user's instructions (default: relay every
   finding). Rewrite each finding as direct maintainer-to-contributor
   feedback:
   - courteous, concrete, actionable; no internal jargon;
   - NEVER mention the proxy issue, marvin, or Meridian tracking internals —
     but the AI origin IS disclosed, via the standard footer in step 4;
   - keep blocking/non-blocking framing ("needs fixing before merge" vs
     "optional/nit").

3. **Map to diff lines.** For each finding with a file:line, check the line is
   part of the PR diff (`gh pr diff <M> --repo <upstream>`; inline comments
   can only attach to diff lines, RIGHT side for additions). Findings on lines
   outside the diff go in the review body with a `path:line` reference
   instead.

4. **Post ONE review** (atomic — summary + inline comments together):

   ```sh
   gh api "repos/<upstream>/pulls/<M>/reviews" -X POST \
     -f body="<summary>" -f event="<EVENT>" \
     --input - <<'JSON'   # or build comments[] with -f comments[][path]= ...
   JSON
   ```

   REST shape: `{body, event, comments: [{path, line, side: "RIGHT", body}]}`
   (use `start_line`+`line` for multi-line). `event`: `REQUEST_CHANGES` when
   relaying any blocking finding, else `COMMENT` — overridable by the user's
   instructions. Never `APPROVE` from this skill; approval is a separate
   deliberate act.

   The review body MUST end with the AI-generation disclaimer footer:

   ```
   ---
   *This review was AI-generated, and reviewed by a maintainer before posting.*
   ```

5. **Bookkeeping.**
   - Proxy stage → **Contributor** (ball is with them now):

     ```sh
     # item id: issue -> projectItems (project 1), then:
     gh api graphql -f query='mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){projectV2Item{id}}}' \
       -f p=PVT_kwDOC7YMCM4BU68p -f i="$ITEM" \
       -f f=PVTSSF_lADOC7YMCM4BU68pzhYZEwY -f o=39c05a50
     ```

   - Note the relay on the proxy issue for the audit trail:
     `gh issue comment N --repo meridianlabs-ai/inspect_ai --body "Relayed upstream as <review url> (<X> inline, <Y> in body). Awaiting contributor."`

6. **Report.** Review URL, what was relayed vs. dropped (and why), inline vs.
   body placement, stage set. The hourly sync brings the proxy back to Human
   Review when the contributor responds — posting this review also updates
   your last-activity timestamp, which is exactly what that detector compares
   against.

## Cautions

- Outward-facing: everything posted lands on a public PR under the user's
  name — and invoking this skill IS the authorization to post: the maintainer
  reviews the findings on the proxy before invoking, so compose and post
  directly, no preview step. Stop and ask only when something is genuinely
  unresolvable: no findings comment on the proxy, instructions that contradict
  each other, or a finding that no longer matches the PR's current state.
- Do not edit the contributor's PR, push to their branch, or touch labels /
  assignees upstream.
