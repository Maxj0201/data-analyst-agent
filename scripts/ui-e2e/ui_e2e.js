// Headless E2E of the Streamlit UI against the dry-run API with every feature flag on
// (see README.md here). Exits non-zero on the first failed check.
//   run 1: sidebar sample + example question -> timeline -> Approve -> report, cost, raw trace
//   run 2: upload a CSV -> type a question -> Deny with a note -> denial in the timeline
// usage: node ui_e2e.js <csv> <out_dir> [<api log, to cross-check decisions via the API>]
const fs = require('fs');
const http = require('http');
const { chromium } = require('playwright');

const UI = process.env.UI || 'http://127.0.0.1:8601';
const API = process.env.API || 'http://127.0.0.1:8100';
const [CSV, OUT, API_LOG] = process.argv.slice(2);
const T = 60000;

function check(ok, what) {
  if (!ok) throw new Error(`check failed: ${what}`);
  console.log(`ok - ${what}`);
}

async function text(page) {
  return page.locator('body').innerText();
}

async function approvalCard(page, run) {
  await page.getByText(/Approve\s*run_sql\?/).waitFor({ timeout: T });
  await page.locator('code:visible').filter({ hasText: 'SELECT' }).first().waitFor({ timeout: T });
  await page.getByRole('button', { name: 'Deny' }).waitFor();
  const body = await text(page);
  check(/EXPLAIN|estimate|rows/i.test(body), `${run}: approval reason shown`);
  check(/Approval required/.test(body), `${run}: approval_required step in the timeline`);
  check(/Plan/.test(body), `${run}: plan step streamed`);
  check(/Routed to L[123]/.test(body), `${run}: route step streamed`);
  check(/Metric definitions/.test(body), `${run}: semantic_retrieval step streamed`);
  check(/run_sql/.test(body), `${run}: tool call streamed`);
  await page.screenshot({ path: `${OUT}/${run}-approval.png`, fullPage: true });
}

async function finished(page, run) {
  await page.getByRole('tab', { name: 'Report' }).waitFor({ timeout: T });
  await page.waitForTimeout(1000);
  await page.getByText(/^Done — /).first().click(); // expand the collapsed timeline
  await page.waitForTimeout(500);
  return text(page);
}

async function approveRun(browser) {
  const run = 'approve';
  const page = await browser.newPage({ viewport: { width: 1280, height: 2800 } });
  await page.goto(UI);
  // A sample is preselected; clicking an example question asks it right away.
  await page.getByText('SaaS subscriptions').first().waitFor({ timeout: T });
  await page.getByRole('button', { name: 'Which region has the highest MRR?' }).click();
  await approvalCard(page, run);
  await page.getByRole('button', { name: 'Approve' }).click();

  let body = await finished(page, run);
  check(/run_sql approved \(human\)/.test(body), `${run}: approval decision in the timeline`);
  check(/Critic: pass/.test(body), `${run}: critic verdict in the timeline`);
  check(/Key numbers/.test(body), `${run}: key numbers rendered`);
  check(body.includes('North America'), `${run}: report answers the question`);
  check((await page.locator('[data-testid="stImage"] img').count()) >= 1, `${run}: chart inline`);
  await page.screenshot({ path: `${OUT}/${run}-report.png`, fullPage: true });

  await page.getByRole('tab', { name: 'Cost & tokens' }).click();
  await page.locator('[data-testid="stDataFrame"]').first().waitFor({ timeout: T });
  body = await text(page);
  check(/Input tokens/.test(body) && /Output tokens/.test(body), `${run}: cost panel rendered`);
  await page.screenshot({ path: `${OUT}/${run}-cost.png`, fullPage: true });

  await page.getByRole('tab', { name: 'Raw trace' }).click();
  await page.getByText(/events · expand an event/).waitFor({ timeout: T });
  body = await text(page);
  check(/approval_decision/.test(body) && /llm_call/.test(body), `${run}: raw trace rendered`);
  await page.screenshot({ path: `${OUT}/${run}-trace.png`, fullPage: true });
  await page.close();
}

async function denyRun(browser) {
  const run = 'deny';
  const page = await browser.newPage({ viewport: { width: 1280, height: 2800 } });
  await page.goto(UI);
  await page.getByText('Upload your own data').waitFor({ timeout: T });
  await page.getByText('Upload your own data').click();
  await page.locator('input[type="file"]').setInputFiles(CSV);
  await page.getByText('your upload').waitFor({ timeout: T });
  await page.locator('textarea').fill('which region has the highest mrr');
  await page.getByRole('button', { name: 'Ask', exact: true }).click();
  await approvalCard(page, run);

  const note = page.getByLabel('Note for the agent (optional)');
  await note.fill('too expensive, use a sample');
  await note.press('Enter');
  await page.getByRole('button', { name: 'Deny' }).click();
  const body = await finished(page, run);
  check(
    /run_sql denied \(human\) — too expensive, use a sample/.test(body),
    `${run}: denial with note in the timeline`,
  );
  await page.screenshot({ path: `${OUT}/${run}-done.png`, fullPage: true });
  await page.close();
}

const get = (u) =>
  new Promise((res, rej) =>
    http
      .get(u, (r) => {
        let b = '';
        r.on('data', (c) => (b += c));
        r.on('end', () => res(b));
      })
      .on('error', rej),
  );

(async () => {
  const browser = await chromium.launch();
  try {
    await approveRun(browser);
    await denyRun(browser);
  } finally {
    await browser.close();
  }
  if (!API_LOG) return;
  // Cross-check with the API: the approve POSTs in the log -> each job's decision events.
  const log = fs.readFileSync(API_LOG, 'utf8');
  const jobs = [...new Set([...log.matchAll(/jobs\/(job_[a-z0-9]+)\/approve/g)].map((m) => m[1]))];
  check(jobs.length === 2, 'API saw one approval POST per run');
  const decisions = [];
  for (const j of jobs) {
    const job = JSON.parse(await get(`${API}/jobs/${j}`));
    const dec = job.trace.filter((e) => e.type === 'approval_decision').map((e) => e.data);
    check(job.status === 'succeeded', `API: ${j} succeeded`);
    decisions.push(...dec.map((d) => [d.tool, d.approved, d.decided_by, d.note || null]));
  }
  check(
    JSON.stringify(decisions) ===
      JSON.stringify([
        ['run_sql', true, 'human', null],
        ['run_sql', false, 'human', 'too expensive, use a sample'],
      ]),
    `API: decisions recorded ${JSON.stringify(decisions)}`,
  );
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
