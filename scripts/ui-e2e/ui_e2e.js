// Headless E2E of the Streamlit UI against the API with approval on (see README.md here):
// upload -> ask -> approval panel -> Approve (run 1) / Deny with a note (run 2).
// usage: node ui_e2e.js <csv> <out_dir> [<api log, to cross-check decisions via the API>]
const { chromium } = require('playwright');
const UI = process.env.UI || 'http://127.0.0.1:8601', API = process.env.API || 'http://127.0.0.1:8100';
const CSV = process.argv[2], OUT = process.argv[3];

async function runOnce(browser, decision) {
  const page = await browser.newPage({ viewport: { width: 1200, height: 1800 } });
  await page.goto(UI);
  await page.getByText('Upload a data file to get started').waitFor({ timeout: 60000 });
  await page.locator('input[type="file"]').setInputFiles(CSV);
  await page.getByText('subscriptions.csv').first().waitFor();
  await page.locator('textarea').fill('which region has the highest mrr');
  await page.getByRole('button', { name: 'Ask' }).click();

  // The stream pauses and the approval panel appears.
  await page.getByText(/Approve\s*run_sql\?/).waitFor({ timeout: 60000 });
  await page.locator('code:visible').filter({ hasText: 'SELECT' }).first().waitFor({ timeout: 30000 });
  await page.getByRole('button', { name: 'Deny' }).waitFor();
  const pending = await page.locator('body').innerText();
  if (!/EXPLAIN|estimate|rows/i.test(pending)) throw new Error('reason missing from panel');
  if (!/SELECT/.test(pending)) throw new Error('SQL missing from panel');
  await page.screenshot({ path: `${OUT}/${decision}-panel.png`, fullPage: true });

  if (decision === 'deny') {
    await page.getByLabel('Note for the agent (optional)').fill('too expensive, use a sample');
    await page.getByLabel('Note for the agent (optional)').press('Enter');
    await page.getByRole('button', { name: 'Deny' }).click();
  } else {
    await page.getByRole('button', { name: 'Approve' }).click();
  }
  // Streaming resumes and the job finishes.
  await page.getByText('Key numbers').waitFor({ timeout: 60000 });
  await page.waitForTimeout(1500);
  await page.getByText('Agent steps').click();
  await page.waitForTimeout(800);
  const body = await page.locator('body').innerText();
  await page.screenshot({ path: `${OUT}/${decision}-done.png`, fullPage: true });
  const jobUrl = page.url();
  await page.close();
  return body;
}

(async () => {
  const browser = await chromium.launch();
  const results = {};
  for (const d of ['approve', 'deny']) results[d] = await runOnce(browser, d);
  await browser.close();
  // Cross-check with the API: last two jobs' approval_decision events.
  const fs = require('fs');
  fs.writeFileSync(`${OUT}/ui-text.json`, JSON.stringify(results, null, 2));
  console.log('UI approve run contains "North America":', results.approve.includes('North America'));
  console.log('UI approve shows approval line:', /run_sql approved \(human\)/.test(results.approve));
  console.log('UI deny shows denial line:', /run_sql denied \(human\) — too expensive, use a sample/.test(results.deny));
  // API cross-check: every job's approval_decision events.
  const http = require('http');
  const get = (u) => new Promise((res, rej) => http.get(u, r => { let b=''; r.on('data', c => b += c); r.on('end', () => res(b)); }).on('error', rej));
  if (!process.argv[4]) return;
  const log = fs.readFileSync(process.argv[4], 'utf8');
  const jobs = [...new Set([...log.matchAll(/jobs\/(job_[a-z0-9]+)\/approve/g)].map(m => m[1]))].slice(-2);
  for (const j of jobs) {
    const ev = await get(`${API}/jobs/${j}/events`);
    const dec = ev.split('\n').filter(l => l.startsWith('data:')).map(l => JSON.parse(l.slice(5))).filter(d => 'decided_by' in d);
    const job = JSON.parse(await get(`${API}/jobs/${j}`));
    console.log(j, job.status, JSON.stringify(dec.map(d => [d.tool, d.approved, d.decided_by, d.note])), 'prompts:', Object.keys(job.result.prompts).join(','));
  }
})().catch(e => { console.error(e); process.exit(1); });
