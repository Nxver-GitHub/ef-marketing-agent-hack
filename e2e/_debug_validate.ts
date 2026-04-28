import { chromium } from "@playwright/test";
(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors: string[] = [];
  page.on("pageerror", (e: Error) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push(`[console.error] ${m.text()}`); });
  page.on("response", async (r) => {
    if (r.status() >= 400 && !r.url().includes("/@vite")) {
      let body = ""; try { body = (await r.text()).slice(0, 400); } catch { /* ignore */ }
      errors.push(`${r.status()} ${r.request().method()} ${r.url()}\n  BODY: ${body}`);
    }
  });

  await page.goto("http://localhost:8080/validate", { waitUntil: "networkidle" });
  await page.waitForTimeout(1000);

  // Fill form
  await page.getByPlaceholder(/Jane Chen/i).fill("Test Demo");
  await page.getByPlaceholder(/ASML/i).fill("Intel Corporation");
  await page.getByPlaceholder(/VP Lithography/i).fill("VP of Engineering");
  await page.getByPlaceholder(/VP Lithography/i).press("Enter");

  console.log("About to submit");
  // Click the submit card
  await page.getByText(/Find & score the lead/i).click();

  // Wait for navigation
  await page.waitForURL(/\/prospect\/.+/, { timeout: 15000 }).catch(e => console.log("NAV FAIL:", e.message));
  console.log("After submit URL:", page.url());

  await page.waitForTimeout(5000);

  const body = (await page.locator("body").innerText()).slice(0, 1500);
  console.log("\n--- body text ---\n" + body);
  console.log("\n--- errors ---");
  errors.forEach(e => console.log(e));

  await browser.close();
})();
