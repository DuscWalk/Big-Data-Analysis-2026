// Exercise actual retry UI/API; fixture mode is explicitly marked as a test double.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
(async () => {
  const fixture = process.env.MOVIELENS_RETRY_FIXTURE ? JSON.parse(await fs.readFile(process.env.MOVIELENS_RETRY_FIXTURE, 'utf8')) : null;
  const session = fixture?.session_id || process.env.MOVIELENS_SESSION_ID;
  const task = fixture?.task_id || process.env.MOVIELENS_TASK_ID;
  const message = fixture?.message_id || process.env.MOVIELENS_MESSAGE_ID;
  const base = fixture?.base_url || process.env.MOVIELENS_BASE_URL || 'http://127.0.0.1:8765';
  const out = process.env.MOVIELENS_BROWSER_OUTPUT;
  assert.ok(session && task && message && out);
  await fs.mkdir(out, { recursive: true });
  const result = { started_at: new Date().toISOString(), mode: fixture ? 'controlled-model-double' : 'live-model-retry', session_id: session, task_id: task, source_message_id: message, attempts: [] };
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    const route = base + '/api/v1/sessions/' + session;
    const original = async id => {
      const all = [];
      for (let offset = 0; ; offset += 100) {
        const response = await page.request.get(route + '/messages?offset=' + offset);
        const body = await response.json(); all.push(...body.items); if (!body.has_more) break;
      }
      return all.find(item => item.message_id === id)?.metadata;
    };
    const beforeTasks = await (await page.request.get(route + '/tasks')).json();
    const before = await original(message);
    assert.equal(before.status, 'failed');
    await page.goto(base + '/?session=' + session);
    await page.waitForFunction(() => document.querySelector('#task-select').options[0]?.value);
    const source = page.locator('[data-message-id="' + message + '"]');
    await source.locator('button.report-view').click();
    await page.waitForFunction(t => document.querySelector('#task-select').value === t && !document.querySelector('#quality').classList.contains('hidden'), task);
    assert.ok(await page.locator('#downloads a').count());
    if (fixture) await page.selectOption('#task-select', fixture.other_task_id);
    let current = message;
    for (let index = 0; index < (fixture ? 2 : 1); index++) {
      const responsePromise = page.waitForResponse(r => r.url() === route + '/messages/' + current + '/retry' && r.request().method() === 'POST', { timeout: 600000 });
      const button = page.locator('[data-message-id="' + current + '"] button.retry-explanation');
      await button.evaluate(el => { el.click(); el.click(); });
      const response = await responsePromise;
      const body = await response.json();
      result.attempts.push({ http_status: response.status(), request: response.request().postDataJSON(), response: body });
      await fs.writeFile(path.join(out, 'progress.json'), JSON.stringify(result, null, 2) + '\n');
      assert.equal(body.retry_of, current);
      assert.deepEqual(body.task_ids, [task]);
      assert.notEqual(body.request_id, before.request_id);
      const expectedStatus = fixture && index === 0 ? 503 : 200;
      assert.equal(response.status(), expectedStatus);
      await page.waitForFunction(id => document.querySelector('[data-message-id="' + id + '"]'), body.message_id);
      await page.waitForFunction(() => !document.querySelector('#send').disabled);
      if (fixture) {
        assert.equal(await page.locator('#task-select').inputValue(), fixture.other_task_id);
        assert.equal(body.validation.requirements.samples[0].offset, 1);
      }
      if (index === 0 && fixture) {
        current = body.message_id;
        await page.reload();
        await page.waitForSelector('[data-message-id="' + current + '"] button.retry-explanation');
        await page.selectOption('#task-select', fixture.other_task_id);
      }
    }
    const last = result.attempts.at(-1).response;
    assert.equal(last.response_origin, 'evidence_rendered');
    const cached = await page.request.post(route + '/messages/' + current + '/retry', { data: result.attempts.at(-1).request });
    assert.deepEqual(await cached.json(), last);
    assert.deepEqual(await original(message), before);
    await page.reload();
    const reply = page.locator('[data-message-id="' + last.message_id + '"]');
    await reply.waitFor(); assert.equal(await reply.locator('.body').textContent(), last.content);
    await reply.screenshot({ path: path.join(out, 'reply.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: path.join(out, 'mobile.png'), fullPage: true });
    const afterTasks = await (await page.request.get(route + '/tasks')).json();
    assert.deepEqual(afterTasks.items.map(t => t.task_id), beforeTasks.items.map(t => t.task_id));
    assert.deepEqual(errors, []);
    result.checks = { report_during_failure: true, original_failure_preserved: true, original_task_bound: true, idempotent_retry: true, refreshed_reply: true, no_new_tasks: true, mobile_no_overflow: true, browser_errors: errors, ...(fixture ? { failure_then_recovery: true, frozen_cursor: true, changed_selection_ignored: true } : {}) };
    result.completed_at = new Date().toISOString();
    await fs.writeFile(path.join(out, 'result.json'), JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify({ mode: result.mode, checks: result.checks, message_id: last.message_id }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
