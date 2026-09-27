// Browser acceptance against a real, already published task. No fixture scores.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const crypto = require("node:crypto");

(async () => {
  const base = process.env.MOVIELENS_BASE_URL || "http://127.0.0.1:8765";
  const session = process.env.MOVIELENS_SESSION_ID || "local-cli";
  const out = process.env.MOVIELENS_BROWSER_OUTPUT || "var/verification/browser";
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error" && !message.text().includes("503") && !message.text().includes("409")) errors.push(message.text()); });
    await fs.mkdir(out, { recursive: true });
    await page.goto(base + "/?session=" + encodeURIComponent(session));
    if (process.env.MOVIELENS_TASK_ID) {
      await page.locator("#task-select option[value='" + process.env.MOVIELENS_TASK_ID + "']").waitFor({ state: "attached" });
      await page.locator("#task-select").selectOption(process.env.MOVIELENS_TASK_ID);
    }
    await page.locator("#quality").waitFor({ state: "visible", timeout: 30000 });
    assert.equal(await page.locator("#metrics .metric").count(), 5);
    const taskId = await page.locator("#task-select").inputValue();
    const taskBase = base + "/api/v1/sessions/" + encodeURIComponent(session) + "/tasks/" + taskId;
    const task = await (await page.request.get(taskBase)).json();
    const report = await (await page.request.get(taskBase + "/quality")).json();
    assert.equal(task.status, "succeeded");
    const expected = report.data.value.after.tables.ratings.rows.toLocaleString("zh-CN");
    assert.ok((await page.locator("#dispositions").innerText()).includes(expected));
    assert.equal(await page.locator("#downloads a").count(), task.artifacts.reduce((n,a) => n+a.files.length,0));
    await page.locator("#load-sample").click();
    await page.locator("#sample").waitFor({ state: "visible" });
    assert.equal(JSON.parse(await page.locator("#sample").innerText()).items.length, 3);
    await page.locator("#load-example").click();
    await page.locator("#examples").waitFor({ state: "visible" });
    assert.ok(JSON.parse(await page.locator("#examples").innerText()).items.length > 0);
    const file = task.artifacts.flatMap(a => a.files).find(f => f.name === "quality.json");
    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("link", { name: "quality.json", exact: true }).click();
    const download = await downloadPromise;
    const saved = path.join(out, "download-quality.json");
    await download.saveAs(saved);
    assert.equal(crypto.createHash("sha256").update(await fs.readFile(saved)).digest("hex"), file.sha256);
    await page.screenshot({ path: path.join(out, "desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2));
    await page.screenshot({ path: path.join(out, "mobile.png"), fullPage: true });
    await page.locator("#new-session").click();
    await page.waitForFunction(() => document.querySelector("#task-select").value === "" && document.querySelector("#quality").classList.contains("hidden"));
    assert.ok((await page.locator("#task-status").innerText()).includes("等待请求"));
    assert.deepEqual(errors, []);
    const result = { session, task_id: taskId, ratings_output: report.data.value.after.tables.ratings.rows,
                     metrics: 5, samples: true, examples: true, download_sha256: file.sha256,
                     desktop: "1440x1000", mobile: "390x844", session_reset: true, browser_errors: errors };
    await fs.writeFile(path.join(out, "result.json"), JSON.stringify(result, null, 2) + "\n");
    console.log(JSON.stringify(result, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
