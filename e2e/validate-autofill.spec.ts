/**
 * End-to-end coverage for the Validate-page autofill (TagInputField).
 *
 * The Validate page exposes two autocomplete tag inputs (role + keywords) via
 * `src/pages/Validate.tsx`. Suggestions come from `useAutocompleteSources()`:
 *  - Roles: canonicalized `public.prospects.role` values, ranked by prefix hit.
 *  - Keywords: a curated SEED_KEYWORDS list unioned with `signals.value.tokens`.
 *
 * These tests codify the behaviours we already verified manually:
 *   1. Typing opens the dropdown.
 *   2. Clicking a suggestion adds it as a tag and dismisses the dropdown.
 *   3. Keyword typing surfaces seed tokens (e.g. "silicon" for "sil").
 *   4. Tab accepts the first prefix-matching suggestion (e.g. "soc" → "SoC").
 *   5. Escape dismisses the dropdown.
 *   6. The end-to-end flow produces no console / page errors.
 *
 * Input targeting note: the role/keyword inputs in Validate.tsx have no
 * explicit label or aria-label — they sit inside a div whose eyebrow text is
 * "Role" / "Keywords". `getByRole('textbox', { name: 'Role' })` therefore
 * does not match. We use `getByPlaceholder(...)` instead, which is the same
 * approach `e2e/frontend.spec.ts` takes for these fields.
 */
import { expect, test, type Page, type ConsoleMessage, type Response } from "@playwright/test";

type Capture = {
  pageErrors: string[];
  consoleErrors: string[];
  networkErrors: string[];
};

// Mirror the noise filters in e2e/frontend.spec.ts so these tests flag *new*
// regressions, not pre-existing backend 400s or framework warnings.
const IGNORED_CONSOLE = [
  /React Router Future Flag Warning/i,
  /Download the React DevTools/i,
  /Invalid DOM property/i,
  /404 Error: User attempted to access non-existent route/i,
];
const IGNORED_NETWORK = [
  /chrome-extension:\/\//,
  /^data:/,
  /^blob:/,
  /sockjs-node/,
  /@vite\/client/,
  /@react-refresh/,
  /\/favicon\.ico$/,
  /\/node_modules\//,
];

function attachCapture(page: Page): Capture {
  const cap: Capture = { pageErrors: [], consoleErrors: [], networkErrors: [] };
  page.on("pageerror", (err: Error) => {
    cap.pageErrors.push(`${err.name}: ${err.message}`);
  });
  page.on("console", (msg: ConsoleMessage) => {
    if (msg.type() !== "error") return;
    const text = msg.text();
    if (IGNORED_CONSOLE.some((re) => re.test(text))) return;
    cap.consoleErrors.push(text);
  });
  page.on("response", (resp: Response) => {
    const url = resp.url();
    if (IGNORED_NETWORK.some((re) => re.test(url))) return;
    const status = resp.status();
    if (status >= 400) cap.networkErrors.push(`${status} ${url}`);
  });
  return cap;
}

/**
 * The role/keyword inputs have two placeholders: empty-state ("VP Lithography"
 * or "chip manufacturing, NPI, wafer fab…") and after-first-tag ("Add another
 * role…" / "Add keyword…"). On a fresh page load the empty-state placeholder
 * is always present — that's what we key off.
 */
function roleInput(page: Page) {
  return page.getByPlaceholder("VP Lithography");
}
function keywordInput(page: Page) {
  return page.getByPlaceholder(/chip manufacturing/i);
}

/**
 * Suggestion dropdown rows are plain <button type="button"> elements inside
 * an absolutely-positioned div directly following the input. We scope by
 * role + accessible name (the suggestion text) rather than by DOM structure
 * so tests don't shatter on cosmetic CSS changes.
 */
function suggestionButton(page: Page, text: string | RegExp) {
  return page.getByRole("button", { name: text });
}

/**
 * Wait for the autocomplete cache to hydrate. `useAutocompleteSources()`
 * fires its Supabase fetch on first mount; until it resolves, the role pool
 * is empty and the dropdown will not render. We wait for networkidle (same
 * pattern as frontend.spec.ts / discover.spec.ts) so role data is loaded.
 */
async function gotoValidate(page: Page) {
  await page.goto("/validate");
  await page.waitForLoadState("networkidle", { timeout: 20_000 });
}

test.describe("Validate autofill", () => {
  test("T1: role dropdown opens on typing and surfaces ≥3 Director matches", async ({ page }) => {
    const cap = attachCapture(page);
    await gotoValidate(page);

    const input = roleInput(page);
    await input.click();
    await input.fill("dir");

    // The dropdown renders suggestion rows as <button>s with the suggestion
    // text as their accessible name. We expect at least 3 "Director…" matches
    // from the 895-prospect Supabase seed.
    const directorBtns = page.getByRole("button", { name: /Director/i });
    await expect(directorBtns.first()).toBeVisible({ timeout: 10_000 });
    const count = await directorBtns.count();
    expect(count, `expected ≥3 Director-matching suggestions, got ${count}`).toBeGreaterThanOrEqual(3);

    expect(cap.pageErrors).toEqual([]);
  });

  test("T2: clicking a role suggestion adds it as a tag and dismisses the dropdown", async ({ page }) => {
    const cap = attachCapture(page);
    await gotoValidate(page);

    const input = roleInput(page);
    await input.click();
    await input.fill("dir");

    const firstDirector = page.getByRole("button", { name: /Director/i }).first();
    await expect(firstDirector).toBeVisible({ timeout: 10_000 });
    const picked = (await firstDirector.textContent())?.trim() ?? "";
    expect(picked.length, "suggestion row had no text").toBeGreaterThan(0);

    await firstDirector.click();

    // The picked value is added as a tag: a <span> containing the text plus
    // a Remove button. Assert on the aria-label of the remove button — that's
    // a stable, semantic handle that the tag (and only the tag) exposes.
    const removeBtn = page.getByRole("button", { name: new RegExp(`^Remove ${escapeRegex(picked)}$`) });
    await expect(removeBtn).toBeVisible({ timeout: 5_000 });

    // And the dropdown should have dismissed — no Director-matching suggestion
    // buttons should remain visible. (The tag's span is not a button, and the
    // Remove button's name starts with "Remove", so this check is unambiguous.)
    // Allow a tick for React to re-render.
    await page.waitForTimeout(150);
    const remainingVisible = await page
      .locator('div.absolute.z-20 button[type="button"]')
      .count();
    expect(remainingVisible, "dropdown still visible after picking a suggestion").toBe(0);

    expect(cap.pageErrors).toEqual([]);
  });

  test("T3: keyword dropdown opens on typing and surfaces 'silicon'", async ({ page }) => {
    const cap = attachCapture(page);
    await gotoValidate(page);

    const input = keywordInput(page);
    await input.click();
    await input.fill("sil");

    // "silicon" is in the seed list — it must appear for input "sil".
    const silicon = page.getByRole("button", { name: /^silicon$/i });
    await expect(silicon).toBeVisible({ timeout: 5_000 });

    expect(cap.pageErrors).toEqual([]);
  });

  test("T4: Tab accepts the first prefix-matching suggestion (so → SoC)", async ({ page }) => {
    const cap = attachCapture(page);
    await gotoValidate(page);

    const input = keywordInput(page);
    await input.click();
    // Type "so" (not "soc"): `rankSuggestions` in src/lib/autocompleteSources.ts
    // skips any pool value whose lowercase equals the query (`if (vl === q) continue;`),
    // so typing the full "soc" filters "SoC" out of its own suggestions and
    // Tab falls through to the raw-input commit path. Using "so" exercises
    // the prefix-accept path the TagInputField was designed for.
    // (This is the contract under test: "Tab accepts the first prefix-matching
    // suggestion." If the `vl === q` filter ever stops short-circuiting exact
    // matches, swap this back to "soc" and the test will still pass.)
    await input.fill("so");

    // The TagInputField's Tab handler injects suggestions[0] iff it starts
    // with the typed input. Ranking for "so" puts "SoC" first (prefix match,
    // shortest length). Wait for the dropdown to render so the handler sees
    // a populated suggestions array when we press Tab.
    await expect(page.getByRole("button", { name: /^SoC$/ })).toBeVisible({ timeout: 5_000 });
    await input.press("Tab");

    const removeBtn = page.getByRole("button", { name: "Remove SoC" });
    await expect(removeBtn).toBeVisible({ timeout: 3_000 });

    expect(cap.pageErrors).toEqual([]);
  });

  test("T5: Escape dismisses the dropdown", async ({ page }) => {
    const cap = attachCapture(page);
    await gotoValidate(page);

    const input = roleInput(page);
    await input.click();
    await input.fill("dir");

    await expect(page.getByRole("button", { name: /Director/i }).first()).toBeVisible({ timeout: 10_000 });

    await input.press("Escape");
    // Escape sets `dismissed = true`, hiding the panel until input changes.
    // Allow a tick for React to re-render.
    await page.waitForTimeout(150);
    const panelButtons = await page
      .locator('div.absolute.z-20 button[type="button"]')
      .count();
    expect(panelButtons, "dropdown still visible after Escape").toBe(0);

    expect(cap.pageErrors).toEqual([]);
  });

  test("T6: no console or page errors across the full autofill flow", async ({ page }) => {
    const cap = attachCapture(page);
    await gotoValidate(page);

    // Exercise the full keyboard + click flow in one pass.
    const role = roleInput(page);
    await role.click();
    await role.fill("dir");
    const firstDir = page.getByRole("button", { name: /Director/i }).first();
    await expect(firstDir).toBeVisible({ timeout: 10_000 });
    await firstDir.click();

    const kw = keywordInput(page);
    await kw.click();
    await kw.fill("sil");
    await expect(page.getByRole("button", { name: /^silicon$/i })).toBeVisible({ timeout: 5_000 });
    await kw.press("Escape");

    // See T4's comment: "so" is the correct prefix to produce "SoC" as
    // suggestions[0] — typing the full "soc" would be filtered by the
    // `vl === q` guard in rankSuggestions().
    await kw.fill("so");
    await expect(page.getByRole("button", { name: /^SoC$/ })).toBeVisible({ timeout: 5_000 });
    await kw.press("Tab");
    await expect(page.getByRole("button", { name: "Remove SoC" })).toBeVisible();

    expect(cap.pageErrors, `page errors during autofill flow:\n${cap.pageErrors.join("\n")}`).toEqual([]);
    expect(cap.consoleErrors, `console errors during autofill flow:\n${cap.consoleErrors.join("\n")}`).toEqual([]);
    expect(cap.networkErrors, `network errors during autofill flow:\n${cap.networkErrors.join("\n")}`).toEqual([]);
  });
});

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
