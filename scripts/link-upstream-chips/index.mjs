#!/usr/bin/env node
// Link each `External` proxy issue to its upstream PR via the GitHub web UI's
// Development panel — the one linking surface with NO public API (see
// design/atlas-tracking.md → External review tracking). Run LOCALLY as
// yourself; first run opens a browser to sign in. Never run this from CI: the
// session state is a full web credential, and automating the web UI outside
// the API is only defensible as personal, attended tooling.
//
// Usage:
//   node index.mjs            # discover pending proxies, link them (headless)
//   node index.mjs --headed   # watch the browser work (debugging)
//   node index.mjs --login    # (re)do the interactive sign-in only
//
// Discovery: open issues labeled `External` in the fork whose
// closedByPullRequestsReferences is empty (no native link yet). The upstream
// PR URL comes from the Atlas "Upstream PR" field, falling back to the
// "Upstream PR: <url>" line in the proxy body. Success is verified via the
// API (the link appears in closedByPullRequestsReferences), not by trusting
// the UI.

import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATE = join(HERE, 'storageState.json');
const REPO = process.env.LINKCHIPS_REPO ?? 'meridianlabs-ai/inspect_ai';
const PROJECT_NUMBER = Number(process.env.LINKCHIPS_PROJECT ?? 1);
const [OWNER, NAME] = REPO.split('/');
const HEADED = process.argv.includes('--headed');
const LOGIN_ONLY = process.argv.includes('--login');

// --pair <issue>:<pr-url> (repeatable): link explicit issue↔PR pairs instead of
// the External-proxy discovery. Needed for fork-internal PRs, whose Fixes refs
// are INERT: GitHub only processes closing keywords on PRs that target the
// default branch, and fork PRs base on pristine `main` while the default is
// `meridian` — so no chip ever forms from the body there.
const PAIRS = [];
for (let i = 2; i < process.argv.length; i++) {
  if (process.argv[i] === '--pair') {
    const [n, url] = process.argv[++i].split(/:(.+)/);
    PAIRS.push({
      issue: Number(n),
      issueUrl: `https://github.com/${REPO}/issues/${n}`,
      pr: url,
    });
  }
}

// A row's pr must be a real PR URL: linkOne derives prNumber from /pull/<n>,
// and a hand-typed field value like "n/a" would otherwise become a row that
// fails every sweep (`No result matching #undefined`) until the field is fixed.
const PR_URL = /\/pull\/\d+/;

const gh = (args) => execFileSync('gh', args, { encoding: 'utf8' });
const graphql = (query, fields = []) =>
  JSON.parse(gh(['api', 'graphql', '-f', `query=${query}`, ...fields]));

function pendingProxies() {
  const q = `query($owner:String!,$name:String!){
    repository(owner:$owner,name:$name){
      issues(labels:["External"],states:OPEN,first:50){
        nodes{
          number url body
          closedByPullRequestsReferences(first:10,includeClosedPrs:true){nodes{url}}
          projectItems(first:10){nodes{
            project{number}
            fieldValueByName(name:"Upstream PR"){
              ... on ProjectV2ItemFieldTextValue{text}
            }
          }}
        }
      }
    }
  }`;
  const out = graphql(q, ['-F', `owner=${OWNER}`, '-F', `name=${NAME}`]);
  const rows = [];
  for (const n of out.data.repository.issues.nodes) {
    if (n.closedByPullRequestsReferences.nodes.length) continue; // chip exists
    const item = n.projectItems.nodes.find((i) => i.project?.number === PROJECT_NUMBER);
    const field = item?.fieldValueByName?.text?.trim();
    if (field && !PR_URL.test(field))
      console.warn(`#${n.number}: "Upstream PR" field is not a PR URL (${JSON.stringify(field)}); ignoring it`);
    const pr =
      (field && PR_URL.test(field) ? field : undefined) ||
      n.body.match(/https:\/\/github\.com\/\S+\/pull\/\d+/)?.[0];
    if (pr) rows.push({ issue: n.number, issueUrl: n.url, pr });
    else console.warn(`#${n.number}: no upstream PR URL in field or body; skipping`);
  }
  return rows;
}

function linkedUrls(issue) {
  const q = `query($owner:String!,$name:String!,$n:Int!){
    repository(owner:$owner,name:$name){
      issue(number:$n){
        closedByPullRequestsReferences(first:10,includeClosedPrs:true){nodes{url}}
      }
    }
  }`;
  const out = graphql(q, ['-F', `owner=${OWNER}`, '-F', `name=${NAME}`, '-F', `n=${issue}`]);
  return out.data.repository.issue.closedByPullRequestsReferences.nodes.map((x) => x.url);
}

function pendingAgentPairs() {
  // Fork-internal agent PRs: open PRs on claude/issue-N-* branches whose
  // issue lacks THIS PR among its native links. Their Fixes refs are correct
  // but inert — closing keywords only register on PRs targeting the DEFAULT
  // branch, and fork agent PRs base on pristine `main` (default `meridian`) —
  // so the chip only ever comes from the Development-panel link.
  const prs = JSON.parse(gh(['api', `repos/${REPO}/pulls?state=open&per_page=100`]));
  const rows = [];
  for (const p of prs) {
    const m = /^claude\/issue-(\d+)-/.exec(p.head.ref);
    if (!m) continue;
    const issue = Number(m[1]);
    try {
      if (linkedUrls(issue).includes(p.html_url)) continue;
    } catch {
      continue; // issue unreadable — skip rather than guess
    }
    rows.push({
      issue,
      issueUrl: `https://github.com/${REPO}/issues/${issue}`,
      pr: p.html_url,
    });
  }
  return rows;
}

function pendingCompanionPairs() {
  // ts-mono companion PRs: viewer halves of fork issues, named by the
  // dev-agent convention (same claude/issue-N-* branch in both repos).
  // Linking them into the anchor issue's Development panel is the one
  // visible cross-repo surface (the sync reads companions by convention;
  // the chip is for humans). Cross-repo Development links only ever come
  // from this sweep.
  const TS_MONO = process.env.LINKCHIPS_TSMONO ?? 'meridianlabs-ai/ts-mono';
  let prs;
  try {
    prs = JSON.parse(gh(['api', `repos/${TS_MONO}/pulls?state=open&per_page=100`]));
  } catch {
    return []; // repo unreadable — companions are a nice-to-have
  }
  const rows = [];
  for (const p of prs) {
    const m = /^claude\/issue-(\d+)-/.exec(p.head.ref);
    if (!m) continue;
    // ts-mono's OWN dev agent also mints claude/issue-N-* branches where N
    // is a ts-mono issue — a companion is only claimed when the SAME branch
    // exists on the fork (the actual convention: one branch name, two repos).
    try {
      execFileSync('gh', ['api', `repos/${REPO}/branches/${encodeURIComponent(p.head.ref)}`], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'], // expected 404s stay silent
      });
    } catch {
      continue; // no fork twin — a ts-mono-native branch, not a companion
    }
    const issue = Number(m[1]);
    try {
      if (linkedUrls(issue).includes(p.html_url)) continue;
    } catch {
      continue; // no such fork issue — the branch name is a coincidence
    }
    rows.push({
      issue,
      issueUrl: `https://github.com/${REPO}/issues/${issue}`,
      pr: p.html_url,
    });
  }
  return rows;
}

function pendingPromotions() {
  // Promotions: fork issues whose Atlas "Upstream PR" field is set (the
  // promote skill's bookkeeping) but whose Development panel lacks the
  // UPSTREAM link itself. Invisible to pendingProxies (no External label)
  // and to pendingAgentPairs (the fork review PR is closed as superseded
  // by the promotion) — observed: #96 parked at Sign-off, panel carrying
  // only the superseded fork PR link, so an any-link-exists check would
  // wrongly skip it: verify the SPECIFIC upstream URL like the agent-pair
  // path does. Discovery is the board, via a paginated three-field
  // GraphQL query — `gh project item-list` pulls every field of every
  // item, which both trips the secondary rate limit and overflows
  // execFileSync's 1MB buffer (a swallowed overflow reads as "nothing
  // pending"). Live items only: the sync clears Stage on every close
  // path, so a null Stage means Done/closed, where a missing chip no
  // longer matters.
  const rows = [];
  let after = '';
  for (;;) {
    const q = `query($org:String!,$num:Int!,$after:String){
      organization(login:$org){ projectV2(number:$num){
        items(first:100, after:$after){
          pageInfo{hasNextPage endCursor}
          nodes{
            up: fieldValueByName(name:"Upstream PR"){
              ... on ProjectV2ItemFieldTextValue{text}}
            stage: fieldValueByName(name:"Stage"){
              ... on ProjectV2ItemFieldSingleSelectValue{name}}
            content{ ... on Issue{number repository{nameWithOwner}}}
          }}}}}`;
    let out;
    try {
      // -f (raw string) for org and the opaque cursor: -F magic-types
      // digits-only / true / false / null values, which the String
      // variables would then reject. Only num is genuinely an Int.
      out = graphql(q, ['-f', `org=${OWNER}`, '-F', `num=${PROJECT_NUMBER}`,
                        ...(after ? ['-f', `after=${after}`] : [])]);
    } catch (e) {
      console.warn(`promotion discovery failed (board unreadable): ${e.message ?? e}`);
      return rows; // partial results beat silence; re-swept next run
    }
    const page = out.data.organization.projectV2.items;
    for (const n of page.nodes) {
      const pr = (n.up?.text ?? '').trim();
      if (!pr || !n.stage?.name) continue;
      const issue = n.content?.number;
      if (!issue || n.content?.repository?.nameWithOwner !== REPO) continue;
      if (!PR_URL.test(pr)) {
        console.warn(`#${issue}: "Upstream PR" field is not a PR URL (${JSON.stringify(pr)}); skipping`);
        continue;
      }
      try {
        if (linkedUrls(issue).includes(pr)) continue; // upstream chip exists
      } catch {
        continue; // issue unreadable — skip rather than guess
      }
      rows.push({ issue, issueUrl: `https://github.com/${REPO}/issues/${issue}`, pr });
    }
    if (!page.pageInfo.hasNextPage) break;
    after = page.pageInfo.endCursor;
  }
  return rows;
}

async function loggedInUser(page) {
  return page
    .evaluate(() => document.querySelector('meta[name="user-login"]')?.content ?? '')
    .catch(() => '');
}

async function ensureLogin(context) {
  const page = await context.newPage();
  await page.goto('https://github.com', { waitUntil: 'domcontentloaded' });
  if (await loggedInUser(page)) {
    await page.close();
    return true;
  }
  if (!HEADED && !LOGIN_ONLY) {
    console.error('Not signed in. Run `node index.mjs --login` first.');
    await page.close();
    return false;
  }
  console.log('Sign in to GitHub in the browser window (5-minute window)...');
  await page.goto('https://github.com/login', { waitUntil: 'domcontentloaded' });
  const deadline = Date.now() + 5 * 60 * 1000;
  while (Date.now() < deadline) {
    if (await loggedInUser(page)) {
      await context.storageState({ path: STATE });
      console.log(`Signed in; session saved to ${STATE}`);
      await page.close();
      return true;
    }
    await page.waitForTimeout(2000);
  }
  console.error('Timed out waiting for sign-in.');
  await page.close();
  return false;
}

async function firstVisible(locators) {
  for (const loc of locators) {
    try {
      const el = loc.first();
      // waitFor actually waits; isVisible({timeout}) ignores the timeout and
      // returns immediately — with a slow-rendering panel (the 2026-08
      // SelectPanel) that exhausted every variant before anything appeared.
      await el.waitFor({ state: 'visible', timeout: 1500 });
      return el;
    } catch {
      /* try next */
    }
  }
  return null;
}

async function linkOne(page, { issue, issueUrl, pr }) {
  const prNumber = pr.match(/\/pull\/(\d+)/)?.[1];
  const preexisting = linkedUrls(issue); // baseline for wrong-link detection
  if (preexisting.includes(pr)) return; // already linked — replaying the UI
  // flow would find the option SELECTED and clicking it would toggle it OFF
  await page.goto(issueUrl, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500); // let the React sidebar hydrate

  // Open the Development panel editor (selector variants across UI versions).
  const gear = await firstVisible([
    page.getByRole('button', { name: /edit development/i }),
    page.locator('button[aria-label*="Development" i]'),
    page.locator('section:has(h3:has-text("Development")) button'),
    page.locator('div:has(> h3:has-text("Development")) button'),
    page.locator('[data-testid*="development" i] button'),
  ]);
  if (!gear) throw new Error('Development panel gear not found — run with --headed to inspect');
  await gear.click();

  const dialog = page.getByRole('dialog').last();
  const search = await firstVisible([
    // 2026-08 Primer SelectPanel: the search is a combobox in an anchored
    // overlay ("Search pull requests"), no longer a dialog-scoped textbox.
    page.getByRole('combobox', { name: /search pull requests/i }),
    dialog.getByRole('combobox', { name: /search/i }),
    dialog.getByRole('textbox'),
    dialog.locator('input[placeholder*="Search" i]'),
    dialog.locator('input[type="text"]'),
  ]);
  if (!search) throw new Error('Development dialog search box not found');
  await search.fill(pr);
  await page.waitForTimeout(2000); // search debounce

  // STRICT match only: the result must carry the exact PR number. Never click
  // an arbitrary first option — a wrong link is far worse than no link (this
  // exact bug linked a proxy to an unrelated fork PR once; see git history).
  const numRe = new RegExp(`#${prNumber}(\\D|$)`);
  const result = await firstVisible([
    dialog.getByRole('option', { name: numRe }),
    // SelectPanel renders options in the overlay, outside the dialog node
    page.getByRole('option', { name: numRe }),
    dialog.locator(`label:has-text("#${prNumber}")`),
  ]);
  if (!result) throw new Error(`No result matching #${prNumber} for ${pr} — not clicking anything`);
  const label = (await result.textContent())?.trim() ?? '';
  if (!numRe.test(label)) throw new Error(`Result label ${JSON.stringify(label)} does not match #${prNumber}`);
  try {
    await result.click({ timeout: 10000 });
  } catch {
    // Primer SelectPanel virtualizes the option list; when the panel
    // already carries a link (pinned above the results — promotions keep
    // their superseded fork-PR link), the matching row can sit outside the
    // overlay's scroll area, where Playwright's auto-scroll gives up
    // ("element is outside of the viewport"; observed on #96). The label
    // was strictly verified above and the API poll below is the ground
    // truth — wrong links are detected, never assumed — so a forced click
    // is safe here.
    await result.scrollIntoViewIfNeeded().catch(() => {});
    // force still computes mouse coordinates, so an off-viewport row fails
    // identically; dispatch the DOM event instead — no viewport involved,
    // and the API poll below still decides success.
    await result.dispatchEvent('click');
  }

  // Persist: some variants have an explicit button, others save on close.
  const apply = await firstVisible([
    dialog.getByRole('button', { name: /^(apply|done|save)$/i }),
  ]);
  if (apply) await apply.click();
  else await page.keyboard.press('Escape');

  // Ground truth: poll the API for the link — and detect a WRONG link (some
  // other URL newly appearing), which must be reported, not just retried.
  const before = new Set(preexisting);
  for (let i = 0; i < 6; i++) {
    await page.waitForTimeout(2500);
    const now = linkedUrls(issue);
    if (now.includes(pr)) return;
    const wrong = now.filter((u) => !before.has(u));
    if (wrong.length)
      throw new Error(
        `WRONG LINK on #${issue}: got ${wrong.join(', ')} instead of ${pr} — unlink it manually in the Development panel`,
      );
  }
  throw new Error(`UI flow completed but the link never appeared for #${issue}`);
}

function dedupe(rows) {
  // The populations overlap: a pending External proxy with the "Upstream PR"
  // field set is found by BOTH pendingProxies and pendingPromotions. One row
  // per issue+pr pair — a second linkOne pass on the same pair is at best a
  // wasted UI round and at worst toggles the fresh link back off.
  const seen = new Set();
  return rows.filter((r) => {
    const key = `${r.issue} ${r.pr}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

const rows = LOGIN_ONLY
  ? []
  : PAIRS.length
    ? PAIRS.filter((r) => {
        if (linkedUrls(r.issue).includes(r.pr)) {
          console.log(`skip    #${r.issue} (already linked to ${r.pr})`);
          return false;
        }
        return true;
      })
    : dedupe([...pendingProxies(), ...pendingAgentPairs(), ...pendingCompanionPairs(), ...pendingPromotions()]);
if (!LOGIN_ONLY && rows.length === 0) {
  console.log('Nothing pending — every target already has its chip.');
  process.exit(0);
}

const browser = await chromium.launch({ headless: !HEADED && !LOGIN_ONLY });
const context = await browser.newContext(
  existsSync(STATE) ? { storageState: STATE } : {},
);
try {
  if (!(await ensureLogin(context))) process.exit(1);
  if (LOGIN_ONLY) process.exit(0);
  const page = await context.newPage();
  let failures = 0;
  for (const row of rows) {
    try {
      await linkOne(page, row);
      console.log(`linked  #${row.issue} -> ${row.pr}`);
    } catch (err) {
      failures++;
      console.error(`FAILED  #${row.issue}: ${err.message}`);
    }
  }
  await context.storageState({ path: STATE }); // keep the session fresh
  process.exit(failures ? 1 : 0);
} finally {
  await browser.close();
}
