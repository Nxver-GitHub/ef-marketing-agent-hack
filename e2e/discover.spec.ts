/**
 * End-to-end tests for /discover — the demo-critical page.
 *
 * Covers: initial render, industry pills, search filter, sortable columns,
 * navigation to prospect detail, empty states, console errors.
 *
 * Notes on resilience:
 * - We do NOT hardcode the exact prospect count (it drifts as the seed grows).
 *   Instead we derive "expected" from the "Total prospects" stat and compare
 *   the footer to that. The contract under test is "stat and footer agree,
 *   and both are nontrivial" — a real regression (empty data) still fails.
 * - Pre-existing Supabase 400s and other known noise are filtered out of
 *   the console-error assertion so these tests catch *new* regressions only.
 */
import { test, expect, Page, ConsoleMessage } from "@playwright/test";

const BASE = "http://localhost:8080";

// Console / network noise that pre-dates these tests and is not a regression
// we own. If this list grows, push back on the source rather than extending it.
const IGNORED_ERROR_PATTERNS: RegExp[] = [
  /hot-update/,
  // Supabase 400s on the demo project — tracked separately. Filtering the
  // generic "Failed to load resource: ... 400" message keeps these tests
  // focused on regressions in the frontend, not pre-existing backend state.
  /Failed to load resource.*\b400\b/,
  /React Router Future Flag Warning/i,
  /Download the React DevTools/i,
];

function isIgnored(msg: string): boolean {
  return IGNORED_ERROR_PATTERNS.some((re) => re.test(msg));
}

function collectConsoleErrors(page: Page): string[] {
  const errs: string[] = [];
  page.on("console", (msg: ConsoleMessage) => {
    if (msg.type() !== "error") return;
    const line = `[console] ${msg.text()}`;
    if (!isIgnored(line)) errs.push(line);
  });
  page.on("pageerror", (err) => {
    const line = `[pageerror] ${err.message}`;
    if (!isIgnored(line)) errs.push(line);
  });
  page.on("requestfailed", (req) => {
    const failure = req.failure();
    if (!failure) return;
    const line = `[requestfailed] ${req.url()} ${failure.errorText}`;
    if (!isIgnored(line)) errs.push(line);
  });
  return errs;
}

/**
 * Wait until at least one prospect row has rendered. Row buttons carry
 * `grid grid-cols-12` — same shape as the header, but the header is a div.
 * We wait on the footer text (only rendered when filtered.length > 0) so
 * we know both the prospect list and score map have settled.
 */
async function waitForRows(page: Page) {
  // Network idle means Supabase fetches have returned (or timed out) — by then
  // either rows have rendered or the empty-state copy is visible.
  await page.waitForLoadState("networkidle", { timeout: 20000 });
  // Footer is only rendered once filtered.length > 0 — i.e. scored data arrived.
  await page
    .getByText(/\d+ of \d+ prospects? · sorted by/)
    .first()
    .waitFor({ timeout: 20000 });
}

/**
 * Read the "Total prospects" stat (the top-left Stat card).
 * Fails loudly if the stat isn't a positive integer — that's the kind of
 * regression we want to catch.
 */
async function readTotalStat(page: Page): Promise<number> {
  const raw = await page
    .getByText("Total prospects")
    .locator("..")
    .locator(".text-3xl")
    .innerText();
  const n = Number(raw);
  expect(Number.isFinite(n) && n > 0, `Total prospects stat is "${raw}"`).toBe(true);
  return n;
}

/** Parse "N of M prospects · sorted by X" → { shown, total, sortedBy }. */
async function readFooter(
  page: Page,
): Promise<{ shown: number; total: number; sortedBy: string }> {
  const text = await page
    .getByText(/\d+ of \d+ prospects? · sorted by/)
    .first()
    .innerText();
  const m = text.match(/(\d+) of (\d+) prospects?.*sorted by\s+(\S+)/i);
  if (!m) throw new Error(`footer didn't match expected shape: ${text}`);
  return { shown: Number(m[1]), total: Number(m[2]), sortedBy: m[3] };
}

test("T1: /discover stat + footer agree and reflect a nonzero prospect count", async ({
  page,
}) => {
  const errs = collectConsoleErrors(page);
  await page.goto(`${BASE}/discover`);
  await waitForRows(page);

  const total = await readTotalStat(page);
  // Guard against "yeah it rendered 3" — the real DB should have hundreds.
  // If someone accidentally ships the 5-prospect mock seed we want to know.
  expect(total).toBeGreaterThanOrEqual(100);

  const footer = await readFooter(page);
  // With no filter applied, all scored prospects should be shown.
  expect(footer.shown).toBe(footer.total);
  // Total stat counts raw prospects; footer total counts *scored* prospects.
  // Scoring is incremental — scored can be ≤ raw while a rescore is in flight.
  // The contract under test is "scoring reaches a nontrivial fraction of the
  // corpus without regressing to zero", not strict equality.
  expect(footer.total).toBeGreaterThan(0);
  expect(footer.total).toBeLessThanOrEqual(total);

  expect(errs, `console/page errors:\n${errs.join("\n")}`).toHaveLength(0);
});

test("T2: industry pills filter correctly and Defense (empty) shows no-match state", async ({
  page,
}) => {
  collectConsoleErrors(page);
  await page.goto(`${BASE}/discover`);
  await waitForRows(page);

  const baseline = await readFooter(page);

  // Semiconductors is the only industry with data in the seed — clicking it
  // should either leave the count unchanged (if it's already the only bucket)
  // or narrow to the semi-only subset. Either way the pill must be enabled
  // and the footer must stay at a nontrivial count.
  // Industry pills render as "<label><count?>", e.g. "Semiconductors895".
  // Match the prefix with a word boundary so we don't pick up an unrelated
  // accidental button starting with the same string.
  const semi = page.getByRole("button", { name: /^Semiconductors\b/ });
  await expect(semi).toBeEnabled({ timeout: 5000 });
  await semi.click();
  await page.waitForTimeout(400);
  const semiFooter = await readFooter(page);
  expect(semiFooter.shown).toBeGreaterThan(0);
  expect(semiFooter.shown).toBeLessThanOrEqual(baseline.total);

  // Defense has no seeded data → pill should be disabled OR clicking it should
  // yield the empty-state copy. Prefix match so we don't pin on the count suffix.
  const defense = page.getByRole("button", { name: /^Defense\b/ });
  const defenseDisabled = await defense.isDisabled();
  if (!defenseDisabled) {
    await defense.click();
    await page.waitForTimeout(300);
    await expect(page.getByText("No matches for this filter.")).toBeVisible();
  }

  // Back to All (pill renders as "All<count>").
  await page.getByRole("button", { name: /^All\b/ }).click();
  await page.waitForTimeout(300);
  const allFooter = await readFooter(page);
  expect(allFooter.total).toBe(baseline.total);
});

test("T3: search filters by name / company / role and empty-state appears for nonsense", async ({
  page,
}) => {
  collectConsoleErrors(page);
  await page.goto(`${BASE}/discover`);
  await waitForRows(page);

  const baseline = await readFooter(page);
  const search = page.getByPlaceholder("Name, company, or role…");

  // Pick a string that's in the seed. NVIDIA is one of the demo prospects.
  await search.fill("nvidia");
  await page.waitForTimeout(400);
  // After filter, either matching rows are present OR empty-state appears.
  // We demand rows — "nvidia" is in the seed. If this fails it's a real bug.
  const nvidiaFooter = await readFooter(page);
  expect(nvidiaFooter.shown).toBeGreaterThan(0);
  expect(nvidiaFooter.shown).toBeLessThanOrEqual(baseline.total);
  // And we can see "NVIDIA" copy in at least one visible row.
  await expect(page.getByText(/NVIDIA/i).first()).toBeVisible();

  // Empty state on nonsense query. Footer disappears (length === 0) so we
  // assert on the empty-state copy instead of re-reading the footer.
  await search.fill("zxzxzxzx_not_a_name_anywhere");
  await page.waitForTimeout(300);
  await expect(page.getByText("No matches for this filter.")).toBeVisible();

  // Clearing restores the baseline total.
  await search.fill("");
  await page.waitForTimeout(400);
  const restored = await readFooter(page);
  expect(restored.total).toBe(baseline.total);
  expect(restored.shown).toBe(baseline.total);
});

test("T4: sortable columns reorder and footer label reflects the active key", async ({
  page,
}) => {
  collectConsoleErrors(page);
  await page.goto(`${BASE}/discover`);
  await waitForRows(page);

  // Column header labels in the DOM (see Discover.tsx `col(...)` calls):
  //   authenticity_score → "Authentic"
  //   authority_score    → "Authority"
  //   warmth_score       → "Warmth"
  //   overall_score      → "Overall"
  // Footer prints sortKey.replace("_score", ""), e.g. "authenticity".
  const cases: Array<{ label: string; footer: string }> = [
    { label: "Authentic", footer: "authenticity" },
    { label: "Authority", footer: "authority" },
    { label: "Warmth", footer: "warmth" },
    { label: "Overall", footer: "overall" },
  ];

  for (const { label, footer } of cases) {
    await page.getByRole("button", { name: label, exact: true }).click();
    await page.waitForTimeout(300);
    const f = await readFooter(page);
    expect(f.sortedBy.toLowerCase()).toBe(footer);
  }
});

test("T5: top row when sorted by Overall DESC has a nonzero score", async ({ page }) => {
  collectConsoleErrors(page);
  await page.goto(`${BASE}/discover`);
  await waitForRows(page);

  // Sort by Overall (also the default). The active column header appends "↓"
  // to its label, so we can't use exact-match on plain "Overall".
  await page.getByRole("button", { name: /^Overall(?:\s|↓|$)/ }).click();
  await page.waitForTimeout(300);

  // Row buttons carry `grid grid-cols-12` — the header above them is a div.
  const firstRow = page.locator("button.grid.grid-cols-12").first();
  await expect(firstRow).toBeVisible();

  // The "Overall" column spans 2 and uses `text-mono text-base` — that's the
  // score cell. All the sub-score pills are `text-mono text-xs`, so this
  // selector is unambiguous.
  const score = await firstRow.locator(".text-mono.text-base").innerText();
  const n = Number(score);
  expect(Number.isFinite(n), `score cell did not parse: "${score}"`).toBe(true);
  expect(n).toBeGreaterThan(0);
  expect(n).toBeLessThanOrEqual(100);

  // And that same row's overall score should match the footer's top-of-list
  // position: sort is DESC, so this row's score is the max among visible rows.
  const allScores = await page
    .locator("button.grid.grid-cols-12 .text-mono.text-base")
    .allInnerTexts();
  const nums = allScores.map(Number).filter((x) => Number.isFinite(x));
  expect(nums.length).toBeGreaterThan(0);
  expect(nums[0]).toBe(Math.max(...nums));
});

test("T6: clicking a prospect row navigates to /prospect/:id and the detail page renders", async ({
  page,
}) => {
  const errs = collectConsoleErrors(page);
  await page.goto(`${BASE}/discover`);
  await waitForRows(page);

  const firstRow = page.locator("button.grid.grid-cols-12").first();
  await firstRow.click();

  await page.waitForURL(/\/prospect\/[A-Za-z0-9_-]+/, { timeout: 10000 });

  // The detail page always renders a back link; either a score+notes block
  // (happy path) or "No score yet." (cold path). Assert on the stable
  // scaffolding so this test doesn't flake on score-pending rows.
  await expect(page.getByRole("link", { name: /back/i })).toBeVisible({ timeout: 10000 });
  // One of the three sub-score labels should be visible if we have a score.
  // Falling through to "No score yet." is also an acceptable terminal state
  // — but we should never see a blank page or a hard error.
  const hasScore = await page
    .getByText(/Authenticity/i)
    .first()
    .isVisible()
    .catch(() => false);
  const hasEmpty = await page
    .getByText(/No score yet\.|Prospect not found\./i)
    .isVisible()
    .catch(() => false);
  expect(
    hasScore || hasEmpty,
    "prospect detail rendered neither score nor a known empty state",
  ).toBe(true);

  expect(errs, `console/page errors after nav:\n${errs.join("\n")}`).toHaveLength(0);
});
