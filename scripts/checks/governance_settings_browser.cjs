// Browser acceptance against governance_fixture.py; no live model or Hadoop.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");

(async () => {
  assert.ok(process.env.MOVIELENS_GOVERNANCE_FIXTURE, "Set MOVIELENS_GOVERNANCE_FIXTURE.");
  const fixturePath = process.env.MOVIELENS_GOVERNANCE_FIXTURE;
  const f = JSON.parse(await fs.readFile(fixturePath, "utf8"));
  assert.equal(f.test_double, true);
  const out = process.env.MOVIELENS_BROWSER_OUTPUT || path.join(path.dirname(fixturePath), "browser");
  await fs.mkdir(out, { recursive: false });
  const browser = await chromium.launch({ headless: true });
  const result = { test_double: true, cases: [], started_at: new Date().toISOString() };
  const saveResult = () => fs.writeFile(path.join(out, "result.json"), JSON.stringify(result, null, 2) + "\n");
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const endpoint = f.base_url + "/api/v1/governance-configs";
    const session = f.base_url + "/api/v1/sessions/" + f.session_id;
    const list = async () => (await page.request.get(endpoint)).json();
    const tasks = async () => (await (await page.request.get(session + "/tasks")).json()).items;
    const feedback = () => page.locator("#governance-feedback");
    const waitFeedback = text => page.waitForFunction(text => document.querySelector("#governance-feedback").textContent.includes(text), text);
    const open = async () => {
      await page.locator("#open-governance-settings").click();
      await page.waitForFunction(() => !document.querySelector("#governance-fields").disabled);
    };
    const close = () => page.locator("#close-governance-settings").click();
    const message = async text => {
      const response = page.waitForResponse(r => r.url() === session + "/messages" && r.request().method() === "POST");
      await page.locator("#prompt").fill(text);
      await page.locator("#send").click();
      const reply = await (await response).json();
      assert.equal(reply.status, "completed");
      await page.waitForFunction(() => !document.querySelector("#send").disabled);
      return reply;
    };
    const initial = await list();
    const original = await (await page.request.get(session + "/tasks/" + f.task_id)).json();
    await page.goto(f.base_url + "/?session=" + f.session_id);
    await page.locator("#task-select").selectOption(f.task_id);
    await page.locator("#quality").waitFor({ state: "visible" });
    await open();
    await page.locator("#governance-name").fill("浏览器 180 天");
    for (const flag of ["zip", "title", "encoding"]) await page.locator("#governance-" + flag).check();
    await page.locator("#governance-reference-linked").uncheck();
    await page.locator("#governance-reference").fill("2001-01-01T00:00");
    await page.locator("#governance-window").fill("180");
    await page.locator("#governance-equal-weights").uncheck();
    await page.locator("#governance-weight-ratings").fill("3");
    await page.locator("#governance-save").click();
    await waitFeedback("已保存并选用");
    const saved = (await list()).items.find(item => item.name === "浏览器 180 天");
    assert.equal(saved.configuration.rules.quarantine_zip_warnings, true);
    assert.equal(saved.configuration.rules.quarantine_title_warnings, true);
    assert.equal(saved.configuration.rules.quarantine_encoding_warnings, true);
    assert.equal(saved.configuration.metrics.reference_time, 978307200);
    assert.equal(saved.configuration.metrics.window_seconds, 180 * 86400);
    assert.equal(saved.configuration.metrics.table_weights.ratings, 3);
    assert.deepEqual((await list()).default, initial.default);
    assert.equal((await tasks()).length, 1);
    await page.locator("#governance-dialog").screenshot({ path: path.join(out, "desktop.png") });
    result.cases.push("switches_and_save_without_task");

    // The default button saves an unsaved draft in a single transaction.
    await page.locator("#governance-name").fill("浏览器 90 天默认");
    await page.locator("#governance-window").fill("90");
    await page.locator("#governance-set-default").click();
    await waitFeedback("已设为默认");
    const default90 = (await list()).default.ref;
    assert.notDeepEqual(default90, saved.ref);
    await close();
    await page.reload();
    await page.waitForFunction(() => document.querySelector("#governance-select").options.length >= 4);
    assert.equal(await page.locator("#governance-select").inputValue(), "");
    result.cases.push("one_click_saves_draft_and_default_survives_reload");

    await page.locator("#governance-select").selectOption(saved.ref.version);
    await page.reload();
    await page.waitForFunction(version => document.querySelector("#governance-select").value === version, saved.ref.version);
    const receipt = await message("按选定方案清洗");
    const queuedId = receipt.task_ids.find(id => id !== f.task_id);
    assert.ok(queuedId);
    const queued = await (await page.request.get(session + "/tasks/" + queuedId)).json();
    assert.equal(queued.status, "queued");
    assert.deepEqual(queued.configuration, saved.configuration);
    await page.locator("#task-select").selectOption(queuedId);
    await page.locator("#task-configuration-panel summary").click();
    await page.locator("#task-configuration").waitFor({ state: "visible" });
    assert.ok((await page.locator("#task-configuration").textContent()).includes(saved.ref.version));
    await page.locator("#task-configuration-panel summary").click();
    await page.locator("#task-configuration").waitFor({ state: "hidden" });
    result.cases.push("explicit_selection_queues_exact_snapshot_and_disclosure_toggles");

    const changed = await message("改为 60 天窗口，设为默认，不要运行清洗");
    assert.equal(changed.configuration_change.default_changed, true);
    await page.waitForFunction(() => document.querySelector("#governance-select").value === "");
    await open();
    assert.equal(await page.locator("#governance-window").inputValue(), "60");
    await close();
    assert.equal((await tasks()).length, 2);
    const unchanged = await (await page.request.get(session + "/tasks/" + f.task_id)).json();
    assert.deepEqual(unchanged.configuration, original.configuration);
    assert.deepEqual(unchanged.artifacts, original.artifacts);
    result.cases.push("conversation_change_syncs_form_and_preserves_history");

    await open();
    await page.locator("#governance-train").fill("2004-01-01T00:00");
    await page.locator("#governance-save").click();
    await waitFeedback("请满足");
    assert.equal((await list()).items.length, 4);
    await close();
    await open();
    const latest = await list();
    assert.equal((await page.request.put(endpoint + "/default", { data: { config_ref: initial.default.ref, revision: latest.default.revision } })).status(), 200);
    await page.locator("#governance-window").fill("45");
    await page.locator("#governance-set-default").click();
    await waitFeedback("默认方案已更新");
    assert.equal(await page.locator("#governance-window").inputValue(), "45");
    assert.equal((await list()).items.length, 4);
    result.cases.push("invalid_time_and_stale_default_preserve_draft");

    const failure = async route => route.request().method() === "POST"
      ? route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "测试保存失败" }) }) : route.continue();
    await page.route("**/api/v1/governance-configs", failure);
    await page.locator("#governance-save").click();
    await waitFeedback("测试保存失败");
    assert.equal(await page.locator("#governance-window").inputValue(), "45");
    await page.unroute("**/api/v1/governance-configs", failure);
    const refreshFailure = route => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "测试刷新失败" }) });
    await page.route("**/api/v1/governance-configs?**", refreshFailure);
    await page.locator("#governance-save").click();
    await waitFeedback("已保存方案，但列表刷新失败");
    assert.ok(!(await feedback().textContent()).startsWith("未保存"));
    await page.unroute("**/api/v1/governance-configs?**", refreshFailure);
    result.cases.push("save_failure_keeps_draft_and_refresh_failure_reports_saved");

    await close();
    await page.locator("#new-session").click();
    await page.waitForFunction(id => new URL(location.href).searchParams.get("session") !== id, f.session_id);
    await open();
    assert.equal(await page.locator("#governance-window").inputValue(), "365");
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.ok(await page.locator("#governance-dialog").evaluate(el => el.scrollWidth <= el.clientWidth));
    await page.screenshot({ path: path.join(out, "mobile.png") });
    await close();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
    result.cases.push("new_session_uses_shared_default_and_mobile_has_no_overflow");
    result.browser_errors = errors;
    result.completed_at = new Date().toISOString();
    await saveResult();
    console.log(JSON.stringify(result));
  } catch (error) {
    result.failure = error.message;
    await saveResult();
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
