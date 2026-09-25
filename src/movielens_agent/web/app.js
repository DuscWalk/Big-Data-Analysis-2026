"use strict";
const $ = id => document.getElementById(id);
const state = { session: null, task: null, detail: null, qualityTask: null, sending: false, timer: null, epoch: 0, explained: new Set() };
const statuses = { queued: "排队中", running: "执行中", succeeded: "已完成", failed: "失败", unknown: "状态待核查" };
const stages = { "prepare-inputs": "核对与封装输入", "before-parents": "检查原始主表", "before-ratings": "检查原始评分", "before-metrics": "汇总清洗前指标", "clean-parents": "清洗用户与电影", "clean-ratings": "清洗评分与关联", "after-groups": "检查清洗结果", "after-metrics": "汇总清洗后指标", "verify-and-export": "核对并导出产物", published: "结果已发布" };
const labels = { users: "用户", movies: "电影", ratings: "评分" };
const dimensions = { Accurate: "准确性", Complete: "完整性", Unique: "唯一性", "Up-to-date": "时效性", Consistent: "一致性" };
const number = value => typeof value === "number" ? value.toLocaleString("zh-CN") : "—";
const score = value => typeof value === "number" ? value.toFixed(3) : "不可评价";
const node = (tag, text, className) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (className) e.className = className; return e; };
function notice(text) { $("notice").textContent = text || ""; $("notice").classList.toggle("hidden", !text); }
async function api(path, options = {}) {
  const response = await fetch("/api/v1" + path, { ...options, headers: { "Content-Type": "application/json", ...options.headers } });
  const body = await response.json();
  if (!response.ok) { const error = new Error(body.content || (typeof body.detail === "string" ? body.detail : body.detail?.message) || "请求未完成"); error.status = response.status; error.body = body; throw error; }
  return body;
}
const route = () => "/sessions/" + encodeURIComponent(state.session);
function artifactRoute(ref) { return route() + "/artifacts/" + encodeURIComponent(ref.artifact_id) + "/versions/" + encodeURIComponent(ref.version); }
function resetResult() {
  state.qualityTask = null; state.detail = null;
  if (!state.task) {
    $("task-status").replaceChildren(node("span", "等待请求", "badge"), node("span", "任务受理后会自动刷新进展"));
    $("task-error").classList.add("hidden"); $("attempts-panel").classList.add("hidden");
  }
  $("quality").classList.add("hidden"); $("quality-empty").classList.remove("hidden");
  $("sample").classList.add("hidden"); $("examples").classList.add("hidden");
}
async function openSession(session) {
  clearTimeout(state.timer); state.epoch++; state.session = session; state.task = null; state.explained.clear(); resetResult();
  state.sending = false; $("send").disabled = false; $("send-state").textContent = "Ctrl / ⌘ + Enter 发送";
  localStorage.setItem("movielens-session", session);
  history.replaceState(null, "", "?session=" + encodeURIComponent(session));
  $("session-caption").textContent = "会话 " + session.slice(0, 12);
  await loadMessages(); await refreshTasks();
}
async function newSession() {
  const session = await api("/sessions", { method: "POST", body: JSON.stringify({ title: "数据治理实验" }) });
  notice(""); await openSession(session.session_id);
}
async function loadMessages() {
  const session = state.session, items = [];
  for (let offset = 0; ; offset += 100) {
    const page = await api(route() + "/messages?offset=" + offset);
    if (session !== state.session) return;
    items.push(...page.items); if (!page.has_more) break;
  }
  const box = $("messages"); box.replaceChildren();
  if (!items.length) box.append(node("p", "输入需求，开始这次实验。", "empty"));
  for (const item of items) {
    const container = node("article", undefined, "message " + item.role + (item.status === "failed" ? " failed" : ""));
    container.append(node("div", item.role === "user" ? "你" : "实验助手", "role"));
    container.append(node("div", item.content, "body"));
    const meta = item.metadata || {};
    if (meta.request_id?.startsWith("explain:")) state.explained.add(meta.request_id.slice(8));
    const usedModels = [...new Set((meta.model_calls || []).flatMap(c => c.attempts || []).filter(a => a.status === "completed").map(a => a.model + (a.provider === "backup" ? "（备用）" : "")))];
    if (usedModels.length) container.append(node("div", (meta.response_origin === "application_receipt" ? "任务回执 · 请求模型：" : meta.response_origin === "evidence_rendered" ? "报告事实 · 要点选择模型：" : "回答模型：") + usedModels.join("、"), "trace"));
    if (meta.tool_calls?.length || meta.model_calls?.length) {
      const trace = node("button", "查看调用依据（工具 " + (meta.tool_calls?.length || 0) + " 次）", "secondary trace");
      trace.addEventListener("click", async () => {
        try {
          const result = await api(route() + "/messages/" + encodeURIComponent(item.message_id) + "/calls");
          let view = container.querySelector("pre");
          if (!view) { view = node("pre"); container.append(view); }
          view.textContent = JSON.stringify({ validation: meta.validation, tools: result.items, models: result.models }, null, 2); view.classList.toggle("hidden", false);
        } catch (error) { notice(error.message); }
      });
      container.append(trace);
    }
    if (item.role === "user" && item.status === "processing") container.append(node("div", "正在读取工具与证据…", "trace"));
    box.append(container);
  }
  box.scrollTop = box.scrollHeight;
}
async function send(content) {
  if (state.sending || !content.trim()) return;
  state.sending = true; $("send").disabled = true; $("send-state").textContent = "正在调用模型与工具…"; notice("");
  const session = state.session, selectedTask = state.task;
  const key = "movielens-pending-" + session;
  let previous = null; try { previous = JSON.parse(localStorage.getItem(key)); } catch {}
  const payload = previous?.content === content && previous?.task_id === selectedTask ? previous : {
    request_id: crypto.randomUUID(), content, task_id: selectedTask
  };
  localStorage.setItem(key, JSON.stringify(payload));
  try {
    await api(route() + "/messages", { method: "POST", body: JSON.stringify(payload) });
    localStorage.removeItem(key); if (session === state.session) $("prompt").value = "";
  } catch (error) {
    if (session !== state.session) return;
    notice(error.message);
    if (error.body?.request_id === payload.request_id) { localStorage.removeItem(key); $("prompt").value = ""; }
  } finally {
    if (session === state.session) {
      state.sending = false; $("send").disabled = false; $("send-state").textContent = "Ctrl / ⌘ + Enter 发送";
      await loadMessages(); await refreshTasks();
    }
  }
}
async function refreshTasks() {
  const session = state.session;
  if (!session) return;
  clearTimeout(state.timer);
  try {
    const page = await api(route() + "/tasks");
    if (session !== state.session) return;
    const select = $("task-select"); select.replaceChildren();
    if (!page.items.length) select.append(new Option("尚无任务", ""));
    for (const task of page.items) select.append(new Option(task.task_id.slice(0, 12) + " · " + (statuses[task.status] || task.status), task.task_id));
    const selected = page.items.some(t => t.task_id === state.task) ? state.task : page.items[0]?.task_id || null;
    if (selected !== state.task) { state.task = selected; state.epoch++; resetResult(); }
    select.value = state.task || "";
    if (state.task) await loadTask();
    if (page.items.some(t => ["queued", "running"].includes(t.status))) state.timer = setTimeout(refreshTasks, 2000);
  } catch (error) { notice(error.message); }
}
async function loadTask() {
  const epoch = state.epoch, taskId = state.task;
  const task = await api(route() + "/tasks/" + taskId);
  if (epoch !== state.epoch || taskId !== state.task) return;
  state.detail = task;
  $("task-status").replaceChildren(node("span", statuses[task.status], "badge " + task.status), node("span", stages[task.stage] || "等待后台 worker 认领"));
  $("task-error").textContent = task.error || ""; $("task-error").classList.toggle("hidden", !task.error);
  $("attempts-panel").classList.toggle("hidden", !task.attempts.length); $("attempts").replaceChildren();
  for (const attempt of task.attempts) {
    const item = node("li", (stages[attempt.stage] || attempt.stage) + " · " + (statuses[attempt.status] || attempt.status));
    if (attempt.external_ids.length) item.append(node("div", attempt.external_ids.join(" / "), "footnote"));
    $("attempts").append(item);
  }
  if (task.status !== "succeeded") { resetResult(); state.detail = task; return; }
  if (state.qualityTask !== taskId) {
    const result = await api(route() + "/tasks/" + taskId + "/quality");
    if (epoch !== state.epoch || taskId !== state.task) return;
    renderQuality(result.data.value, task); state.qualityTask = taskId;
  }
  if (!state.sending && !state.explained.has(taskId)) explain(taskId).catch(error => notice(error.message));
}
async function explain(taskId, attempt = 0) {
  if (taskId !== state.task || state.explained.has(taskId)) return;
  const session = state.session;
  state.explained.add(taskId);
  try { await api(route() + "/tasks/" + taskId + "/explanation", { method: "POST", body: "{}" }); }
  catch (error) {
    if (session !== state.session) return;
    if (error.status === 409 && attempt < 15) {
      state.explained.delete(taskId); setTimeout(() => explain(taskId, attempt + 1), 2000); return;
    }
    notice("任务产物已就绪。结果解释：" + error.message);
  }
  if (session === state.session) await loadMessages();
}
function renderQuality(value, task) {
  $("quality-empty").classList.add("hidden"); $("quality").classList.remove("hidden"); $("metrics").replaceChildren();
  for (const [dimension, label] of Object.entries(dimensions)) {
    const before = value.before.overall[dimension].score, after = value.after.overall[dimension].score;
    const row = node("div", undefined, "metric"), title = node("div", undefined, "metric-title");
    const labelNode = node("span", label + " "); labelNode.append(node("small", dimension));
    title.append(labelNode, node("span", score(before) + " → " + score(after), "metric-score")); row.append(title);
    const bars = node("div", undefined, "metric-bars");
    for (const [v, kind] of [[before, "before"], [after, "after"]]) {
      const progress = node("progress", undefined, "bar " + kind); progress.max = 100; progress.value = v === null ? 0 : v;
      progress.setAttribute("aria-label", label + (kind === "before" ? "清洗前 " : "清洗后 ") + score(v)); bars.append(progress);
    }
    row.append(bars); $("metrics").append(row);
  }
  $("dispositions").replaceChildren();
  for (const table of ["users", "movies", "ratings"]) {
    const d = value.after.dispositions[table], row = node("tr");
    const values = [labels[table], number(value.before.tables[table].rows), number(value.after.tables[table].rows), number(d.repaired || 0), number(d.deduplicated || 0), number(d.quarantined || 0)];
    values.forEach(v => row.append(node("td", v))); $("dispositions").append(row);
  }
  const split = value.after.splits;
  const utc = v => new Date(v * 1000).toISOString().replace("T", " ").replace(".000Z", " UTC");
  $("splits").textContent = "训练 " + number(split.train) + " · 验证 " + number(split.validation) + " · 测试 " + number(split.test) +
    "。T1：" + utc(value.configuration.split.train_end) + "；T2：" + utc(value.configuration.split.validation_end) + "。";
  $("method").replaceChildren();
  const reference = value.configuration.metrics;
  $("method").append(node("p", "Accurate / Complete / Unique / Consistent 按三表等权汇总；空表不可评价。时效性只评价评分，以 " +
    utc(reference.reference_time) + " 为参照，回看 " + reference.window_seconds / 86400 + " 天。", "method-line"));
  for (const table of ["users", "movies", "ratings"]) {
    for (const dimension of Object.keys(dimensions)) {
      const a = value.before.tables[table].metrics[dimension], b = value.after.tables[table].metrics[dimension];
      $("method").append(node("p", labels[table] + " · " + dimensions[dimension] + "：" +
        (a.population === undefined ? "不适用" : number(a.passed) + " / " + number(a.population)) + " → " +
        (b.population === undefined ? "不适用" : number(b.passed) + " / " + number(b.population)), "method-line"));
    }
    const reasons = value.after.reasons[table];
    for (const [reason, count] of Object.entries(reasons)) $("method").append(node("p", labels[table] + " · " + reason + "：" + number(count) + " 次（标签可重叠）", "method-line"));
  }
  value.limitations.forEach(text => $("method").append(node("p", text, "method-line")));
  $("downloads").replaceChildren();
  for (const artifact of task.artifacts) for (const file of artifact.files) {
    const link = node("a", file.name); link.href = "/api/v1" + artifactRoute(artifact.ref) + "/download?file_name=" + encodeURIComponent(file.name);
    link.setAttribute("download", file.name); $("downloads").append(link);
  }
  $("versions").textContent = JSON.stringify({ task_id: task.task_id, input: value.input_ref, cleaned: value.cleaned_ref, configuration: value.config_refs }, null, 2);
  $("issue-rule").replaceChildren();
  for (const table of ["users", "movies", "ratings"]) for (const reason of Object.keys(value.after.reasons[table])) {
    $("issue-rule").append(new Option(labels[table] + " · " + reason, table + "/" + reason));
  }
}
$("chat-form").addEventListener("submit", event => { event.preventDefault(); send($("prompt").value).catch(error => notice(error.message)); });
$("prompt").addEventListener("keydown", event => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); $("chat-form").requestSubmit(); } });
$("suggest-run").addEventListener("click", () => { $("prompt").value = "请使用默认规则清洗 MovieLens 1M，评估前后五维质量，并说明处理的问题与局限。"; $("prompt").focus(); });
$("suggest-explain").addEventListener("click", () => { $("prompt").value = "请读取当前任务的实际指标，解释分数变化和数据处置，并说明哪些问题仍无法核实。"; $("prompt").focus(); });
$("new-session").addEventListener("click", () => newSession().catch(error => notice(error.message)));
$("refresh").addEventListener("click", () => { state.qualityTask = null; refreshTasks(); loadMessages().catch(error => notice(error.message)); });
$("task-select").addEventListener("change", () => { state.task = $("task-select").value || null; state.epoch++; resetResult(); if (state.task) loadTask().catch(error => notice(error.message)); });
$("load-sample").addEventListener("click", async () => {
  try {
    const epoch = state.epoch, artifact = state.detail.artifacts.find(item => item.kind === "cleaned_dataset");
    const value = await api(artifactRoute(artifact.ref) + "?mode=sample&limit=3&file_name=" + $("sample-table").value + ".jsonl");
    if (epoch !== state.epoch) return;
    $("sample").textContent = JSON.stringify(value.data.value, null, 2); $("sample").classList.remove("hidden");
  } catch (error) { notice(error.message); }
});
$("load-example").addEventListener("click", async () => {
  try {
    const epoch = state.epoch, artifact = state.detail.artifacts.find(item => item.kind === "quality_report");
    const value = await api(artifactRoute(artifact.ref) + "?mode=examples&limit=3&reason=" + encodeURIComponent($("issue-rule").value));
    if (epoch !== state.epoch) return;
    $("examples").textContent = JSON.stringify(value.data.value, null, 2); $("examples").classList.remove("hidden");
  } catch (error) { notice(error.message); }
});
(async () => {
  const status = await api("/status");
  $("model-state").textContent = status.model_configured ? "模型已配置" + (status.backup_model_name ? " · 备用可选" : "") : "模型尚未配置";
  const requested = new URLSearchParams(location.search).get("session");
  const previous = requested || localStorage.getItem("movielens-session");
  if (previous) {
    try { await api("/sessions/" + encodeURIComponent(previous)); await openSession(previous); return; }
    catch (error) { if (error.status !== 404) throw error; }
  }
  await newSession();
})().catch(error => notice(error.message));
