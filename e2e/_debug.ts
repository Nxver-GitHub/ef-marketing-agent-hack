import { chromium } from "@playwright/test";
(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const requests: string[] = [];
  const errors: string[] = [];
  page.on("request", (r) => requests.push(`${r.method()} ${r.url()}`));
  page.on("response", async (r) => {
    if (r.status() >= 400 && !r.url().includes("/@vite") && !r.url().includes("hot-update")) {
      let body = "";
      try { body = (await r.text()).slice(0, 400); } catch { /* ignore body read failure */ }
      errors.push(`${r.status()} ${r.request().method()} ${r.url()}\n  BODY: ${body}`);
    }
  });
  page.on("pageerror", (e: Error) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push(`[console.error] ${m.text()}`); });

  await page.goto("http://localhost:8080/discover", { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(8000);

  const rowCount = await page.locator("button.grid.grid-cols-12").count();
  const totalStat = await page.locator("text=TOTAL PROSPECTS").locator("..").innerText().catch(()=>"");
  const avgStat = await page.locator("text=AVG SCORE").locator("..").innerText().catch(()=>"");
  const topStat = await page.locator("text=TOP SCORE").locator("..").innerText().catch(()=>"");

  console.log("button.grid rows:", rowCount);
  console.log("stats:", { totalStat, avgStat, topStat });
  console.log("\n--- SUPABASE REQUESTS ---");
  requests.filter(r => r.includes("supabase.co") && !r.includes(".hot-update.")).slice(0,15).forEach(r => console.log(r));
  console.log("\n--- ERRORS ---");
  errors.forEach(e => console.log(e));

  await browser.close();
})();
