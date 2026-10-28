# Ownership

Scope: these files are written for Meridian Labs employees and their agents. Anyone can read them. Nobody outside Meridian is expected to follow them, but we think they are reasonable, good practice.

Rule scope: **Meridian-wide**. Each Meridian person's agents obey these rules. They reach them through `meridian-agents.md`.

This file says who is who, and who tracks CI and canary failures in each repo. It stops two agents from doing the same work.

## Meridian People

All Meridian people have write access to UKGovernmentBEIS/inspect_ai and to all meridianlabs-ai repos.

| Name | GitHub | Notes |
|---|---|---|
| Eric Patey | `epatey` | |
| JJ Allaire | `jjallaire` | |
| Charles Teague | `dragonstyle` | |
| Ransom Richardson | `ransomr` | |
| Matt Brandly | `brandly` | |
| Craig Walton | `craig-meridian` | |
| Alexandra Abbas | `alexandraabbas` | |
| Kai Fronsdal | `kaifronsdal` | |
| Noah Chen | `jchengjr` | |
| Marvin | `i-am-marvin`, `meridian-marvin[bot]` | Ransom's agent; watches #slow-tests-results |
| Canary Wrangler | none (Slack only) | Eric's agent; handles canary alerts for Eric's repos |
| Cletus | none (Slack only) | Eric's agent (chief of staff) |

## Repo tracking

Each repo can have many people who can help. Each repo has exactly one tracker. The tracker is the person whose agent tracks the repo's CI and canary failures. (Provisional, from Eric)

Some repos may be out of scope. The team has not decided which.

| Repo | Tracker (one person) | Can help | Alert channels |
|---|---|---|---|
| UKGovernmentBEIS/inspect_ai | Eric | jjallaire, dragonstyle, epatey | #canaries, #slow-tests-results (see note) |
| actions | Ransom (tentative) | | |
| agents | Ransom (tentative) | | |
| aws-config | Eric (tentative) | dragonstyle, epatey | |
| canaries | Eric or Matt (to pick) | | #canaries |
| condorai | JJ (tentative) | | |
| flow_steps_demo | Alexandra (tentative) | | |
| inspect-docs | JJ (tentative) | | |
| inspect-hardening | Matt (tentative) | | |
| inspect-skills | Alexandra (tentative) | dragonstyle, alexandraabbas, epatey | |
| inspect-viz-example-logs | JJ (tentative) | | |
| inspect_ai (fork) | | | |
| inspect_ai_dev | JJ (tentative) | | |
| inspect_charxiv | Noah (tentative) | | |
| inspect_flow | Ransom (tentative) | ransomr, jjallaire, dragonstyle | #canaries |
| inspect_harbor | Alexandra (tentative) | alexandraabbas, jjallaire, dragonstyle | #canaries |
| inspect_petri | Kai (tentative) | kaifronsdal, jjallaire, dragonstyle | #canaries |
| inspect_petri_dev | JJ (tentative) | | |
| inspect_ranges | JJ (tentative) | | #canaries |
| inspect_sandboxes | Alexandra (tentative) | alexandraabbas, jjallaire, dragonstyle | #canaries |
| inspect_scout | Eric | jjallaire, dragonstyle, epatey | #canaries |
| inspect_sentinel | Eric | jjallaire, dragonstyle, epatey | #canaries |
| inspect_steward | | | #canaries |
| inspect_swarm | Ransom (tentative) | ransomr, jjallaire, dragonstyle | #canaries |
| inspect_swe | Eric | jjallaire, dragonstyle, epatey | #canaries, #inspect-swe-nightly |
| inspect_viz | Eric (tentative) | dragonstyle, jjallaire, epatey | #canaries |
| inspect_vscode | Eric | dragonstyle, epatey, brandly | #canaries |
| meridian-docs | Craig (tentative) | | |
| meridian-fundraising | JJ (tentative) | | |
| morpheus | | | |
| petri_bloom | Kai (tentative) | jjallaire, dragonstyle, kaifronsdal | #canaries |
| petri_dish | Kai (tentative) | kaifronsdal, jjallaire, dragonstyle | #canaries |
| release-testing | Eric (tentative) | | |
| remote-worker-dashboard | Alexandra (tentative) | | |
| scout-hal-import | JJ (tentative) | | |
| sentinel-presentation | JJ (tentative) | | |
| situational-awareness | Charles (tentative) | | |
| ts-mono | JJ (tentative) | | |
| ts-mono-test | Eric (tentative) | | |
| veevals | | | |
| website | Charles (tentative) | | |

Tentative names are proposals. The team agrees on trackers in review.

Repos are in `meridianlabs-ai` unless the name has an owner. "Can help" comes from each repo's CODEOWNERS.

Note on `UKGovernmentBEIS/inspect_ai`: Ransom (Marvin) tracks #slow-tests-results. Eric's agents do not act there. Open: one tracker for each repo, or one for each signal.

To set or change a tracker, open a PR that changes this table. The new tracker approves it.

## Required

### CI and canary alerts

All rules in this section: Provisional, from Eric.

- Before you act on a red CI run or a red canary alert, read this file.
- Act only if your person is the tracker for that repo.
- If your person is not the tracker, do not act. Two exceptions: the tracker asks you, or you have facts that the tracker does not have. Then give only an FYI in the alert thread.
- A repo with an empty Tracker cell has no tracker. If you see an alert for that repo, tell your person. Do not take ownership.
