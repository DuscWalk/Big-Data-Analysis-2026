// Check a live followup, or replay a persisted real reply with MOVIELENS_MESSAGE_ID.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
(async () => {
  const base = process.env.MOVIELENS_BASE_URL || "http://127.0.0.1:8765";
  const session = process.env.MOVIELENS_SESSION_ID;
  const task = process.env.MOVIELENS_TASK_ID;
  assert.ok(session && task, "Set MOVIELENS_SESSION_ID and MOVIELENS_TASK_ID.");
  const out = process.env.MOVIELENS_BROWSER_OUTPUT || "var/verification/evidence-reply-browser";
  await fs.mkdir(out, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const result = { session_id: session, task_id: task, started_at: new Date().toISOString() };
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(base + "/?session=" + encodeURIComponent(session));
    await page.waitForFunction(() => document.querySelector("#task-select").options.length > 0);
    await page.selectOption("#task-select", task);
    await page.waitForFunction(() => !document.querySelector("#quality").classList.contains("hidden"));
    const prefix = base + "/api/v1/sessions/" + session;
    const replay = process.env.MOVIELENS_MESSAGE_ID;
    let assistantIndex = -1;
    if (replay) {
      result.mode = "persisted-reply";
      const items = [];
      for (let offset = 0; ; offset += 100) {
        const http = await page.request.get(prefix + "/messages?offset=" + offset);
        assert.equal(http.status(), 200);
        const value = await http.json();
        items.push(...value.items);
        if (!value.has_more) break;
      }
      const assistants = items.filter(item => item.role === "assistant");
      assistantIndex = assistants.findIndex(item => item.message_id === replay);
      assert.ok(assistantIndex >= 0, "The requested persisted reply is not in this session.");
      result.response = assistants[assistantIndex].metadata;
    } else {
      result.mode = "live-followup";
      const response = page.waitForResponse(r => r.url() === prefix + "/messages" && r.request().method() === "POST", { timeout: 600000 });
      await page.locator("#prompt").fill("请只解释本任务时效性：给出实际分子、分母、正确分数、百分比及历史窗口，不要重新清洗。");
      await page.locator("#send").click();
      const http = await response;
      result.http_status = http.status();
      result.response = await http.json();
    }
    await fs.writeFile(path.join(out, "response.json"), JSON.stringify(result, null, 2) + "\n");
    if (!replay) assert.equal(result.http_status, 200);
    assert.equal(result.response.status, "completed");
    assert.equal(result.response.response_origin, "evidence_rendered");
    if (!replay) assert.ok(result.response.validation.sections.includes("freshness"));
    // Repeated questions can produce identical text. Wait for the new message
    // identity so an older answer cannot satisfy the check during a UI reload.
    const replySelector = '[data-message-id="' + result.response.message_id + '"]';
    const ready = async () => page.waitForFunction(({ text, selector }) => document.querySelector(selector + ' .body')?.textContent === text, { text: result.response.content, selector: replySelector });
    await ready();
    const last = page.locator(replySelector);
    assert.ok((await last.textContent()).includes("报告事实 · 要点选择模型"));
    await last.locator("button.trace").click();
    const trace = JSON.parse(await last.locator("pre").textContent());
    assert.deepEqual(trace.validation, result.response.validation);
    assert.ok(trace.tools.some(item => item.call_id === trace.validation.summary_call_id));
    const v2 = ["quality-facts-v2", "quality-facts-v3"].includes(trace.validation.policy);
    if (v2) {
      assert.equal(trace.validation.answer_characters, [...result.response.content].length);
      assert.ok(trace.validation.application_call_ids.length > 0);
      for (const id of trace.validation.application_call_ids) {
        assert.ok(trace.tools.some(item => item.call_id === id && item.requested_by === "application"));
      }
      if (trace.validation.requirements.brief) {
        assert.ok([...result.response.content].length <= trace.validation.requirements.max_characters);
        assert.ok(trace.validation.sections.length <= trace.validation.requirements.max_sections);
      }
    }
    await page.screenshot({ path: path.join(out, "desktop.png"), fullPage: true });
    await page.reload();
    await ready();
    assert.ok((await page.locator(replySelector).textContent()).includes("报告事实 · 要点选择模型"));
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: path.join(out, "mobile.png"), fullPage: true });
    assert.deepEqual(errors, []);
    result.checks = { rendered_reply: true, persisted_validation: true, current_summary_call: true,
                      ...(v2 ? { application_evidence_source: true, answer_length_metadata: true } : {}),
                      reloaded_reply: true, mobile_no_overflow: true, browser_errors: errors };
    result.completed_at = new Date().toISOString();
    await fs.writeFile(path.join(out, "result.json"), JSON.stringify(result, null, 2) + "\n");
    console.log(JSON.stringify({ checks: result.checks, message_id: result.response.message_id }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
