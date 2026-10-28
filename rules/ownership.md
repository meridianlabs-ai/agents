# Ownership

Scope: these files are written for Meridian Labs employees and their agents. Anyone can read them. Nobody outside Meridian is expected to follow them, but we think they are reasonable, good practice.

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

| Repo | Tracker (one person) | Tracking agent | Can help | Has canary | Alert channel |
|---|---|---|---|---|---|
| UKGovernmentBEIS/inspect_ai | Eric | Canary Wrangler | jjallaire, dragonstyle, epatey | yes | #canaries |
| actions | Ransom (tentative) | | | no | |
| agents | Ransom (tentative) | | | no | |
| aws-config | Eric (tentative) | | dragonstyle, epatey | no | |
| canaries | Eric or Matt (to pick) | | | no | #canaries |
| condorai | JJ (tentative) | | | no | |
| flow_steps_demo | Alexandra (tentative) | | | no | |
| inspect-docs | JJ (tentative) | | | no | |
| inspect-hardening | Matt (tentative) | | | no | |
| inspect-skills | Alexandra (tentative) | | dragonstyle, alexandraabbas, epatey | no | |
| inspect-viz-example-logs | JJ (tentative) | | | no | |
| inspect_ai (fork) | | | | no | |
| inspect_ai_dev | JJ (tentative) | | | no | |
| inspect_charxiv | Noah (tentative) | | | no | |
| inspect_flow | Ransom (tentative) | | ransomr, jjallaire, dragonstyle | yes | #canaries |
| inspect_harbor | Alexandra (tentative) | | alexandraabbas, jjallaire, dragonstyle | yes | #canaries |
| inspect_petri | Kai (tentative) | | kaifronsdal, jjallaire, dragonstyle | yes | #canaries |
| inspect_petri_dev | JJ (tentative) | | | no | |
| inspect_ranges | JJ (tentative) | | | yes | #canaries |
| inspect_sandboxes | Alexandra (tentative) | | alexandraabbas, jjallaire, dragonstyle | yes | #canaries |
| inspect_scout | Eric | Canary Wrangler | jjallaire, dragonstyle, epatey | yes | #canaries |
| inspect_sentinel | Eric | Canary Wrangler | jjallaire, dragonstyle, epatey | yes | #canaries |
| inspect_steward | | | | yes | #canaries |
| inspect_swarm | Ransom (tentative) | | ransomr, jjallaire, dragonstyle | yes | #canaries |
| inspect_swe | Eric | Canary Wrangler | jjallaire, dragonstyle, epatey | yes | #canaries |
| inspect_viz | Eric (tentative) | | dragonstyle, jjallaire, epatey | yes | #canaries |
| inspect_vscode | Eric | Canary Wrangler | dragonstyle, epatey, brandly | yes | #canaries |
| meridian-docs | Craig (tentative) | | | no | |
| meridian-fundraising | JJ (tentative) | | | no | |
| morpheus | | | | no | |
| petri_bloom | Kai (tentative) | | jjallaire, dragonstyle, kaifronsdal | yes | #canaries |
| petri_dish | Kai (tentative) | | kaifronsdal, jjallaire, dragonstyle | yes | #canaries |
| release-testing | Eric (tentative) | | | no | |
| remote-worker-dashboard | Alexandra (tentative) | | | no | |
| scout-hal-import | JJ (tentative) | | | no | |
| sentinel-presentation | JJ (tentative) | | | no | |
| situational-awareness | Charles (tentative) | | | no | |
| ts-mono | JJ (tentative) | | | no | |
| ts-mono-test | Eric (tentative) | | | no | |
| veevals | | | | no | |
| website | Charles (tentative) | | | no | |

Tentative names are proposals. The team agrees on trackers in review.

Repos are in `meridianlabs-ai` unless the name has an owner. "Can help" comes from each repo's CODEOWNERS.

Note on `UKGovernmentBEIS/inspect_ai`: Ransom (agent: Marvin) tracks the scheduled slow tests in #slow-tests-results. Eric's agents do not act in that channel. We have not yet decided between one tracker for each repo and one tracker for each signal.

To set or change a tracker, open a PR that changes this table. The new tracker approves it.

## Required

### CI and canary alerts

All rules in this section: Provisional, from Eric.

- Before you act on a red CI run or a red canary alert, read this file.
- Act only if your person is the tracker for that repo.
- If your person is not the tracker, do not act. Two exceptions: the tracker asks you, or you have facts that the tracker does not have. Then give only an FYI in the alert thread.
- A repo with an empty Tracker cell has no tracker. If you see an alert for that repo, tell your person. Do not take ownership.
