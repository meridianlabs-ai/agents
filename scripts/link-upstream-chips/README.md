# link-upstream-chips

Gives each `External` proxy issue its clickable **linked-PR chip** by driving
the GitHub web UI's Development panel — the one linking surface with **no
public API** (verified; see
[design/atlas-tracking.md → External review tracking](../../design/atlas-tracking.md)).
A closing keyword would need to be in the *external contributor's PR body*,
which we don't edit; comments don't link (tested).

## Why local-only — never CI

- The saved session (`storageState.json`) is a **full GitHub web credential**
  (no PAT scoping). It stays on your machine and is gitignored.
- Automating the web UI outside the API is defensible as attended personal
  tooling, not as unattended org infrastructure.
- When GitHub ships a linking API, this logic moves into the periodic action
  and this script retires.

## Setup

```sh
cd scripts/link-upstream-chips
npm install
npx playwright install chromium
node index.mjs --login   # one-time interactive GitHub sign-in
```

## Use

```sh
node index.mjs           # link every pending External proxy (headless)
node index.mjs --headed  # watch it work / debug selector breakage
```

Discovery is API-driven: open `External`-labeled issues in the fork with no
existing native link, upstream URL from the Atlas **Upstream PR** field (body
template as fallback). Each link is **verified via the API** afterward — the
script only reports `linked` when `closedByPullRequestsReferences` shows it.

Sessions last ~2 weeks; rerun `--login` when prompted. If GitHub reshuffles
the Development panel markup, run `--headed` and adjust the selector lists in
`linkOne()`.
