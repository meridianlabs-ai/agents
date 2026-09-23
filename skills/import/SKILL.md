---
name: import
description: Import an upstream inspect_ai issue into the fork — /import <upstream-issue-number-or-url> mirrors the UKGovernmentBEIS issue as a fork issue (snapshot body + machine-readable upstream link) and adds it to Atlas as Todo, so @auto can work it. Does NOT kick @auto — the user comments @auto with their own guidance.
---

# Import an upstream issue into the fork

The inverse of /promote's entry point: an issue reported in
`UKGovernmentBEIS/inspect_ai` (upstream) that we want the agents to work.
The agents have no upstream write access and the workflows live on the fork
(`meridianlabs-ai/inspect_ai`), so `@auto` needs a fork issue to anchor on.
This skill creates that mirror. See design/atlas-tracking.md → "Imported
upstream issues".

## Fast path

Run the script that lives next to this skill (substitute this skill's base
directory). Add `--dry-run` to preview:

```sh
bash <skill-base-dir>/import.sh <upstream-issue-number-or-url> [--dry-run]
```

One invocation does everything: rejects PRs (those are the External-proxy
flow, owned by the Atlas sync) and closed issues; dedupes against fork
issues already carrying the upstream URL (open and closed — a Done import
is never recreated); creates the fork issue — title copied, body =
machine-readable first line `Upstream issue: <url>` plus a snapshot of the
upstream body with bare `#N` refs qualified to `UKGovernmentBEIS/...#N` (in
fork context they'd rebind to unrelated fork issues); adds it to Atlas with
`Status = Todo` (Stage stays unset — the agent post-step owns Todo → Agent).

The copied title and snapshot are **de-fanged**: `@claude`, `@i-am-marvin`,
`@auto` and `@review` become `` `claude` `` etc. and the loops' `<!-- … -->`
markers are split, case-insensitively — the same rewrite the land composite
applies to every body the machine account republishes. The fork issue is
created under *your* login, and the fork's stubs fire on an opened issue
whose body or title contains a phrase, with the gate authorizing the actor:
without the rewrite, an outsider's `@auto <task>` in the upstream text would
run as your directive the moment you imported it (Claude Security 4629154).
The gate in claude.yml also declines body/title text triggers on an opened
issue whose first line is `Upstream issue:`, so an import never starts an
agent on its own.

Exit codes: **0** ok (report the `OK …` line; on a dup it points at the
existing import); **4** not an open upstream issue (closed, a PR, or
missing).

## What it deliberately does NOT do

- **Kick `@auto`.** The user typically has guidance to add, so the handoff
  is theirs: report the fork issue URL and remind them to comment
  `@auto <guidance>` on it.
- **Comment on the upstream issue.** Upstream is not ours; the maintainer
  can note "working on this" personally if they want visibility.
- **Sync anything afterward.** The body is a snapshot; canonical discussion
  stays upstream. No comment/state mirroring.

## The loop closes at promotion

promote.sh reads the `Upstream issue:` header — the body's first line with
the `---` rule below it, as this skill writes them, and only when the fork issue's author is a trusted
login or has write access on the fork (the import is the operator's issue;
anyone can file a fork issue carrying the same line, or paste it below the
`---` rule) — and adds a second closing ref — bare `Fixes #<upstream-N>` —
to the upstream PR body it creates (bare refs resolve fine there: upstream
PRs base on upstream `main`). So the
upstream issue gets the native linked-PR chip and auto-closes when the PR
merges; the fork issue closes via the hourly sync as usual. Creation-time
only — promote never edits an adopted (pre-existing) upstream PR's body, so
if you promoted before importing-related fixes existed, add the `Fixes`
line to the upstream PR body by hand.

## Report

Fork issue URL, upstream issue URL, and whether it was created or already
present. Remind the user: `@auto <guidance>` on the fork issue starts the
work.
