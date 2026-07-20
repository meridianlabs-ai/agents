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
    const pr =
      item?.fieldValueByName?.text?.trim() ||
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
      if (await el.isVisible({ timeout: 1500 })) return el;
    } catch {
      /* try next */
    }
  }
  return null;
}

async function linkOne(page, { issue, issueUrl, pr }) {
  const prNumber = pr.match(/\/pull\/(\d+)/)?.[1];
  const preexisting = linkedUrls(issue); // baseline for wrong-link detection
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
    dialog.locator(`label:has-text("#${prNumber}")`),
  ]);
  if (!result) throw new Error(`No result matching #${prNumber} for ${pr} — not clicking anything`);
  const label = (await result.textContent())?.trim() ?? '';
  if (!numRe.test(label)) throw new Error(`Result label ${JSON.stringify(label)} does not match #${prNumber}`);
  await result.click();

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
    : pendingProxies();
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
