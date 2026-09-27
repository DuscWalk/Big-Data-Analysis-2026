// Opt-in real browser/model check against an existing completed task; no new jobs.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");

(async () => {
  const base = process.env.MOVIELENS_BASE_URL || "http://127.0.0.1:8765";
  const session = process.env.MOVIELENS_SESSION_ID;
  const task = process.env.MOVIELENS_TASK_ID;
  const out = process.env.MOVIELENS_BROWSER_OUTPUT || "var/verification/conversation-browser";
  assert.ok(session && task, "Set MOVIELENS_SESSION_ID and MOVIELENS_TASK_ID.");
  await fs.mkdir(out, { recursive: true });
  const resultPath = path.join(out, "result.json");
  assert.equal(await fs.access(resultPath).then(() => true, () => false), false, "Use a new result directory.");
  const browser = await chromium.launch({ headless: true });
  const result = { session_id: session, task_id: task, started_at: new Date().toISOString(), cases: [] };
  const save = () => fs.writeFile(resultPath, JSON.stringify(result, null, 2) + "\n");
  try {
    const page = await browser.newPage({ viewport: { width: 1366, height: 1000 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const prefix = base + "/api/v1/sessions/" + session;
    const tasks = async () => (await (await page.request.get(prefix + "/tasks")).json()).items.map(t => t.task_id).sort();
    const before = await tasks();
    await page.goto(base + "/?session=" + encodeURIComponent(session));
    await page.waitForFunction(() => document.querySelector("#task-select").options.length > 0);
    await page.selectOption("#task-select", task);
    await page.waitForFunction(() => !document.querySelector("#quality").classList.contains("hidden"));
    const cases = [
      ["greeting", "你好", "general"],
      ["concept", "什么是 MapReduce？请面向初学者简短解释，不要解释当前任务的报告。", "general"],
      ["status", "当前任务完成了吗？只告诉我状态。", "status"],
      ["report", "你好，请简短解释本任务的评分损失，不要重新清洗。", "report"],
      ["sample", "给出一个 ratings/R14_VALID_KEY_CONFLICT 的来源样例。", "sample"],
      ["next", "再给一个", "sample"],
      ["thanks", "谢谢，先聊到这里", "general"],
    ];
    const selected = process.env.MOVIELENS_CONVERSATION_CASES?.split(",");
    for (const [name, question, kind] of cases) {
      if (selected && !selected.includes(name)) continue;
      const start = Date.now();
      const pending = page.waitForResponse(r => r.url() === prefix + "/messages" && r.request().method() === "POST", { timeout: 300000 });
      await page.locator("#prompt").fill(question);
      await page.locator("#send").click();
      const http = await pending;
      const reply = await http.json();
      const record = { name, question, http_status: http.status(), duration_ms: Date.now() - start, reply };
      result.cases.push(record);
      await save();
      assert.equal(http.status(), 200, name + ": request failed");
      assert.equal(reply.status, "completed", name);
      assert.ok(reply.task_ids.includes(task), name + ": task context lost");
      assert.ok(!reply.tool_calls.some(c => c.name === "governance.run"), name + ": unexpected job");
      const selector = '[data-message-id="' + reply.message_id + '"]';
      await page.waitForFunction(({ selector, content }) => document.querySelector(selector + " .body")?.textContent === content,
        { selector, content: reply.content });
      if (kind === "general") {
        assert.equal(reply.response_origin, "model", name);
        assert.equal(reply.tool_calls.length, 0, name);
        assert.equal(reply.model_calls.length, 1, name);
        assert.ok(!reply.content.includes("已发布质量报告："), name);
        if (["greeting", "thanks"].includes(name)) {
          assert.ok(!/报告|时效|当前任务|[a-f0-9]{32}/.test(reply.content), name + ": unsolicited task recap");
        }
      } else if (kind === "status") {
        assert.equal(reply.response_origin, "model");
        assert.ok(reply.tool_calls.some(c => c.name === "tasks.get"));
        assert.ok(!reply.tool_calls.some(c => c.name === "artifacts.get"));
      } else {
        assert.equal(reply.response_origin, "evidence_rendered", name);
        assert.equal(reply.validation.task_id, task, name);
        if (kind === "report") assert.ok(reply.validation.sections.includes("rating_loss"));
        else {
          const check = reply.validation.sample_checks[0];
          assert.equal(check.reason, "ratings/R14_VALID_KEY_CONFLICT");
          assert.equal(check.requested_count, 1);
          if (name === "next") {
            const prior = result.cases.find(c => c.name === "sample").reply;
            assert.equal(reply.validation.requirements.continuation_of, prior.message_id);
            assert.equal(check.offset, prior.validation.sample_checks[0].count);
          }
        }
        record.calls = await (await page.request.get(prefix + "/messages/" + reply.message_id + "/calls")).json();
        const summary = record.calls.items.find(c => c.call_id === reply.validation.summary_call_id);
        assert.ok(summary && summary.status === "completed");
        assert.equal(summary.arguments.mode, "summary");
        assert.deepEqual(summary.arguments.artifact_ref, reply.validation.quality_ref);
        await page.locator(selector + " button.trace").click();
        await page.locator(selector + " pre").waitFor({ state: "visible" });
        await page.locator(selector + " button.trace").click();
        await page.locator(selector + " pre").waitFor({ state: "hidden" });
      }
      await page.locator(selector).screenshot({ path: path.join(out, name + ".png") });
      record.passed = true;
      await save();
      console.log(JSON.stringify({ name, origin: reply.response_origin, tool_calls: reply.tool_calls.length,
        model_calls: reply.model_calls.length, duration_ms: record.duration_ms }));
    }
    assert.deepEqual(await tasks(), before);
    result.no_new_tasks = true;
    const last = result.cases.at(-1).reply;
    await page.reload();
    await page.locator('[data-message-id="' + last.message_id + '"]').waitFor();
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
    result.browser_errors = errors;
    result.reloaded_reply = result.mobile_no_overflow = true;
    result.completed_at = new Date().toISOString();
    await save();
  } catch (error) {
    result.failure = error.message;
    await save();
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
