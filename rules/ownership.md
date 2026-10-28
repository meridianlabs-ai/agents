# Ownership

Scope: these files are written for Meridian Labs employees and their agents. Anyone can read them. Nobody outside Meridian is expected to follow them, but we think they are reasonable, good practice.

This file says who is who, and who tracks CI and canary failures in each repo. It stops two agents from doing the same work.

## People

| Name | GitHub | Role |
|---|---|---|
| Eric Patey | `epatey` | Co-maintainer, Meridian employee |
| JJ Allaire | `jjallaire` | Co-maintainer. Meridian employee: to confirm |
| Charles Teague | `dragonstyle` | Co-maintainer, Meridian employee |
| Ransom Richardson | `ransomr` | Co-maintainer, Meridian employee |
| Matt Brandly | `brandly` | Co-maintainer. Meridian employee: to confirm |
| Craig Walton | `craig-meridian` | Meridian employee: to confirm |
| Alexandra Abbas | `alexandraabbas` | Meridian employee: to confirm |
| Kai Fronsdal | `kaifronsdal` | Meridian employee: to confirm |
| noahwc | `jchengjr` | Meridian employee: to confirm |
| Marvin | `i-am-marvin`, `meridian-marvin[bot]` | Agent (Ransom's) |
| Canary Wrangler, Cletus | none (Slack only) | Agents (Eric's) |

"Co-maintainer" means write access or more on `UKGovernmentBEIS/inspect_ai`. Other people on that repo are not in this table.

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
| inspect_charxiv | noahwc (tentative) | | | no | |
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
