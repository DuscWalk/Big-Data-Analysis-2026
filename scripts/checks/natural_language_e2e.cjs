// Opt-in live check: calls the configured model and submits one full Hadoop task.
// Requires the local API, Hadoop and an independent worker to be running.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
(async () => {
  const base = process.env.MOVIELENS_BASE_URL || "http://127.0.0.1:8765";
  const expectedProvider = process.env.MOVIELENS_EXPECT_PROVIDER;
  const out = process.env.MOVIELENS_E2E_OUTPUT || "var/verification/natural-language";
  await fs.mkdir(out, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const result = { started_at: new Date().toISOString(), base_url: base };
  const save = name => fs.writeFile(path.join(out, name), JSON.stringify(result, null, 2) + "\n");
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const configured = await (await page.request.get(base + "/api/v1/status")).json();
    result.model_configuration = { provider: configured.model_provider, model: configured.model_name };
    if (expectedProvider) assert.equal(configured.model_provider, expectedProvider);
    await page.goto(base);
    await page.waitForFunction(() => document.querySelector("#session-caption").textContent.startsWith("会话 "));
    result.session_id = await page.evaluate(() => localStorage.getItem("movielens-session"));
    await save("started.json");
    const prefix = base + "/api/v1/sessions/" + result.session_id;
    const trace = async name => {
      result[name + "_calls"] = await (await page.request.get(prefix + "/messages/" + result[name].message_id + "/calls")).json();
      if (expectedProvider) {
        const attempts = result[name].model_calls.flatMap(call => call.attempts);
        assert.ok(attempts.length > 0);
        assert.ok(attempts.every(attempt => attempt.provider === expectedProvider));
      }
    };
    const validateExplanation = reply => {
      assert.equal(reply.response_origin, "evidence_rendered");
      assert.equal(reply.validation.task_id, result.task_id);
      const report = result.task.artifacts.find(a => a.kind === "quality_report");
      assert.deepEqual(reply.validation.quality_ref, report.ref);
      assert.ok(reply.tool_calls.some(call => call.call_id === reply.validation.summary_call_id && call.name === "artifacts.get" && call.status === "completed"));
    };
    const explanationPromise = page.waitForResponse(r => r.url().startsWith(prefix + "/tasks/") && r.url().endsWith("/explanation") && r.request().method() === "POST", { timeout: 1800000 });
    // Attach a rejection observer immediately, even if initial submission fails.
    explanationPromise.catch(() => {});
    const submitted = page.waitForResponse(r => r.url() === prefix + "/messages" && r.request().method() === "POST", { timeout: 300000 });
    await page.locator("#prompt").fill("请使用默认规则清洗 MovieLens 1M，评估清洗前后的五维质量，并说明处理的问题与局限。");
    await page.locator("#send").click();
    const submission = await submitted;
    result.submission_http = submission.status();
    result.submission = await submission.json();
    result.task_id = result.submission.task_ids?.[0];
    await trace("submission");
    await save("submitted.json");
    console.log(JSON.stringify({ event: "submitted", session_id: result.session_id, task_id: result.task_id, status: result.submission.status }));
    assert.equal(result.submission_http, 200);
    assert.ok(result.task_id);
    assert.equal(result.submission.response_origin, "application_receipt");
    assert.equal(result.submission.task_ids.length, 1);
    const explanation = await explanationPromise;
    result.explanation_http = explanation.status();
    result.explanation = await explanation.json();
    await trace("explanation");
    await save("explained.json");
    assert.equal(result.explanation_http, 200);
    const task = await (await page.request.get(prefix + "/tasks/" + result.task_id)).json();
    assert.equal(task.status, "succeeded");
    result.task = task;
    validateExplanation(result.explanation);
    for (const section of ["scores", "dispositions", "freshness", "time_splits", "limitations"]) {
      assert.ok(result.explanation.validation.sections.includes(section), "Missing full explanation section: " + section);
    }
    result.quality = await (await page.request.get(prefix + "/tasks/" + result.task_id + "/quality")).json();
    const followup = page.waitForResponse(r => r.url() === prefix + "/messages" && r.request().method() === "POST", { timeout: 300000 });
    await page.locator("#prompt").fill("请依据本任务证据解释：为什么清洗后四项质量达到 100 分，时效性却仍然很低？这些分数能否证明数据真实？请简短回答，不要重新清洗。");
    await page.locator("#send").click();
    const reply = await followup;
    result.followup_http = reply.status();
    result.followup = await reply.json();
    await trace("followup");
    await save("followup.json");
    assert.equal(result.followup_http, 200);
    validateExplanation(result.followup);
    assert.ok(result.followup.validation.sections.includes("freshness"));
    assert.ok(result.followup.validation.sections.some(s => ["metric_method", "limitations"].includes(s)));
    const tasks = await (await page.request.get(prefix + "/tasks")).json();
    assert.equal(tasks.items.length, 1);
    result.checks = { one_task: true, receipt: true, succeeded: true,
      automatic_full_explanation: true, scoped_followup: true,
      expected_provider: expectedProvider || null };
    result.browser_errors = errors;
    assert.deepEqual(errors, []);
    await page.waitForFunction(() => document.querySelectorAll("#messages .assistant").length >= 3);
    await page.screenshot({ path: path.join(out, "conversation-and-results.png"), fullPage: true });
    result.completed_at = new Date().toISOString();
    await save("result.json");
    console.log(JSON.stringify({ event: "completed", session_id: result.session_id, task_id: result.task_id,
      tool_calls: ["submission", "explanation", "followup"].map(n => result[n].tool_calls),
      ratings_output: result.quality.data.value.after.tables.ratings.rows, browser_errors: errors }, null, 2));
  } catch (error) {
    result.failure = { type: error.name, message: error.message };
    await save("failed.json");
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
