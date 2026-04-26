// Headless deep-dive of the deployed frontend.
// Captures: per-route network timings, console errors, request waterfall,
// Web Vitals (FCP/LCP/CLS via PerformanceObserver), DOM stats, screenshots.

import { chromium } from 'playwright';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

const BASE = process.env.BASE_URL || 'https://ef-marketing-agent-hack.vercel.app';
const OUT = resolve('test-results/deep-test');
mkdirSync(OUT, { recursive: true });

const ROUTES = ['/', '/validate', '/discover', '/settings'];

const collectVitals = `
new Promise(resolve => {
  const out = { fcp: null, lcp: null, cls: 0, longTasks: 0, longTaskMs: 0 };
  try {
    new PerformanceObserver(list => {
      for (const e of list.getEntries()) {
        if (e.name === 'first-contentful-paint') out.fcp = e.startTime;
      }
    }).observe({ type: 'paint', buffered: true });
    new PerformanceObserver(list => {
      const entries = list.getEntries();
      out.lcp = entries[entries.length - 1]?.startTime ?? out.lcp;
    }).observe({ type: 'largest-contentful-paint', buffered: true });
    new PerformanceObserver(list => {
      for (const e of list.getEntries()) if (!e.hadRecentInput) out.cls += e.value;
    }).observe({ type: 'layout-shift', buffered: true });
    new PerformanceObserver(list => {
      for (const e of list.getEntries()) { out.longTasks++; out.longTaskMs += e.duration; }
    }).observe({ type: 'longtask', buffered: true });
  } catch {}
  setTimeout(() => resolve(out), 2500);
});
`;

async function profileRoute(browser, path) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const reqs = [];
  const errors = [];
  const consoleMsgs = [];

  page.on('request', r => reqs.push({ url: r.url(), method: r.method(), type: r.resourceType(), start: Date.now() }));
  page.on('requestfinished', async r => {
    const i = reqs.findIndex(x => x.url === r.url() && !x.done);
    if (i >= 0) {
      const timing = r.timing();
      const resp = await r.response().catch(() => null);
      reqs[i].done = true;
      reqs[i].status = resp?.status();
      reqs[i].size = (await resp?.body().catch(() => null))?.length ?? null;
      reqs[i].timing = timing;
      reqs[i].durationMs = timing.responseEnd - timing.startTime;
    }
  });
  page.on('requestfailed', r => errors.push({ kind: 'request_failed', url: r.url(), err: r.failure()?.errorText }));
  page.on('pageerror', e => errors.push({ kind: 'page_error', message: e.message, stack: e.stack }));
  page.on('console', m => {
    if (['error', 'warning'].includes(m.type())) consoleMsgs.push({ type: m.type(), text: m.text() });
  });

  const url = BASE + path;
  const t0 = Date.now();
  let nav;
  try {
    nav = await page.goto(url, { waitUntil: 'networkidle', timeout: 30000 });
  } catch (e) {
    errors.push({ kind: 'nav_timeout', message: e.message });
    nav = null;
  }
  const navMs = Date.now() - t0;

  const vitals = await page.evaluate(collectVitals).catch(() => ({}));
  const navTiming = await page.evaluate(() => {
    const e = performance.getEntriesByType('navigation')[0];
    if (!e) return null;
    return {
      domContentLoaded: e.domContentLoadedEventEnd - e.startTime,
      loadEvent: e.loadEventEnd - e.startTime,
      domInteractive: e.domInteractive - e.startTime,
      ttfb: e.responseStart - e.requestStart,
      transferSize: e.transferSize,
      encodedBodySize: e.encodedBodySize,
      decodedBodySize: e.decodedBodySize,
    };
  });
  const dom = await page.evaluate(() => ({
    nodes: document.querySelectorAll('*').length,
    images: document.images.length,
    scripts: document.scripts.length,
    stylesheets: document.styleSheets.length,
    title: document.title,
    h1: document.querySelector('h1')?.innerText ?? null,
    bodyText: document.body.innerText.slice(0, 800),
  }));

  const screenshot = resolve(OUT, `route_${path.replaceAll('/', '_') || 'root'}.png`);
  await page.screenshot({ path: screenshot, fullPage: true }).catch(() => {});

  await ctx.close();

  return {
    path, url, status: nav?.status() ?? null, navMs,
    navTiming, vitals, dom, errors, consoleMsgs,
    requests: reqs.map(r => ({
      url: r.url, type: r.type, status: r.status,
      size: r.size, durationMs: r.durationMs,
    })),
    screenshot,
  };
}

const browser = await chromium.launch();
const results = [];
for (const r of ROUTES) {
  console.log(`\n→ ${r}`);
  const out = await profileRoute(browser, r);
  console.log(`  status=${out.status} navMs=${out.navMs} fcp=${out.vitals.fcp?.toFixed(0)} lcp=${out.vitals.lcp?.toFixed(0)} cls=${out.vitals.cls?.toFixed(3)} reqs=${out.requests.length} errors=${out.errors.length} console=${out.consoleMsgs.length}`);
  results.push(out);
}
await browser.close();

writeFileSync(resolve(OUT, 'report.json'), JSON.stringify({ baseUrl: BASE, capturedAt: new Date().toISOString(), results }, null, 2));
console.log(`\nReport: ${resolve(OUT, 'report.json')}`);
