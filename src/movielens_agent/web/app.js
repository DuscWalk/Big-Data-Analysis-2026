"use strict";
const $ = id => document.getElementById(id);
const state = { session: null, task: null, detail: null, qualityTask: null, sending: false, timer: null, messageTimer: null, epoch: 0, navigation: 0, messageLoad: 0, explained: new Set(), inflight: new Set(), drafts: new Map() };
const statuses = { queued: "排队中", running: "执行中", succeeded: "已完成", failed: "失败", unknown: "状态待核查" };
const stages = { "prepare-inputs": "核对与封装输入", "before-parents": "检查原始主表", "before-ratings": "检查原始评分", "before-metrics": "汇总清洗前指标", "clean-parents": "清洗用户与电影", "clean-ratings": "清洗评分与关联", "after-groups": "检查清洗结果", "after-metrics": "汇总清洗后指标", "verify-and-export": "核对并导出产物", published: "结果已发布" };
const labels = { users: "用户", movies: "电影", ratings: "评分" };
const dimensions = { Accurate: "准确性", Complete: "完整性", Unique: "唯一性", "Up-to-date": "时效性", Consistent: "一致性" };
const number = value => Number.isFinite(value) ? value.toLocaleString("zh-CN") : "—";
const score = value => Number.isFinite(value) ? value.toFixed(3) : "不可评价";
const utc = value => new Date(value * 1000).toISOString().replace("T", " ").replace(".000Z", " UTC");
const sectionLinks = [...document.querySelectorAll(".section-index a")];
const node = (tag, text, className) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (className) e.className = className; return e; };
function notice(text) { $("notice").textContent = text || ""; $("notice").classList.toggle("hidden", !text); }
async function api(path, options = {}) {
  const response = await fetch("/api/v1" + path, { ...options, headers: { "Content-Type": "application/json", ...options.headers } });
  const body = await response.json();
  if (!response.ok) { const error = new Error(body.content || (typeof body.detail === "string" ? body.detail : body.detail?.message) || "请求未完成"); error.status = response.status; error.body = body; throw error; }
  return body;
}
function createDisclosure(button, view, expandedText, load) {
  const collapsedText = button.textContent;
  let loaded = false, loading = false, generation = 0;
  button.type = "button";
  button.setAttribute("aria-controls", view.id);
  const setExpanded = expanded => {
    button.setAttribute("aria-expanded", String(expanded));
    button.textContent = expanded ? expandedText : collapsedText;
    view.classList.toggle("hidden", !expanded);
  };
  const reset = () => {
    // Ignore pending reads from the previous task or sample selection.
    generation++; loaded = false; loading = false;
    view.textContent = ""; view.setAttribute("aria-busy", "false");
    setExpanded(false);
  };
  button.addEventListener("click", async () => {
    const expanded = button.getAttribute("aria-expanded") !== "true";
    setExpanded(expanded);
    if (!expanded || loaded || loading) return;
    const current = generation;
    loading = true;
    view.textContent = "正在读取…"; view.setAttribute("aria-busy", "true");
    try {
      const text = await load();
      if (current !== generation || !button.isConnected) return;
      view.textContent = text; loaded = true;
    } catch (error) {
      if (current !== generation || !button.isConnected) return;
      setExpanded(false); notice(error.message);
    } finally {
      if (current === generation) { loading = false; view.setAttribute("aria-busy", "false"); }
    }
  });
  reset();
  return { reset };
}
const route = () => "/sessions/" + encodeURIComponent(state.session);
function artifactRoute(ref) { return route() + "/artifacts/" + encodeURIComponent(ref.artifact_id) + "/versions/" + encodeURIComponent(ref.version); }
function resetResult(clearTask = false) {
  state.qualityTask = null; state.detail = null;
  if (!state.task || clearTask) {
    $("task-status").replaceChildren(node("span", state.task ? "读取中" : "等待请求", "badge"), node("span", state.task ? "正在读取所选任务" : "任务受理后会自动刷新进展"));
    $("task-error").classList.add("hidden"); $("attempts-panel").classList.add("hidden");
    $("attempts").replaceChildren(); $("attempts").dataset.task = "";
  }
  $("quality").classList.add("hidden"); $("quality-empty").classList.remove("hidden");
  sampleDisclosure.reset(); exampleDisclosure.reset();
  sectionLinks[1].href = "#quality-empty"; sectionLinks[2].href = "#quality-empty";
  for (const id of ["summary-retained", "summary-quarantined", "summary-files"]) $(id).textContent = "—";
  $("summary-input").textContent = "完成任务后显示";
  $("summary-loss").textContent = "保留实际数据处置";
  $("summary-evidence").textContent = "数据、报告与来源";
  $("overview-state").textContent = "等待结果";
  $("context-caption").textContent = "提出清洗请求或实验问题";
}
async function openSession(session) {
  const navigation = ++state.navigation;
  const value = await api("/sessions/" + encodeURIComponent(session));
  if (navigation !== state.navigation) return;
  if (state.session) state.drafts.set(state.session, $("prompt").value);
  clearTimeout(state.timer); clearTimeout(state.messageTimer);
  state.epoch++; state.session = session; state.task = value.active_task_id; state.explained.clear(); resetResult(true);
  $("task-select").replaceChildren(new Option("正在读取任务…", ""));
  $("messages").replaceChildren(node("p", "正在读取会话…", "empty"));
  $("prompt").value = state.drafts.get(session) || "";
  sending(true, "正在读取会话…"); notice("");
  localStorage.setItem("movielens-session", session);
  const url = new URL(location.href);
  url.searchParams.set("session", session);
  history.replaceState(null, "", url.pathname + url.search + url.hash);
  $("session-caption").textContent = "会话 " + session.slice(0, 12);
  try {
    await loadMessages();
    if (navigation !== state.navigation) return;
    await refreshTasks(false);
  } catch (error) {
    if (navigation === state.navigation) sending(state.inflight.has(session));
    throw error;
  }
}
async function newSession() {
  const session = await api("/sessions", { method: "POST", body: JSON.stringify({ title: "数据治理实验" }) });
  notice(""); await openSession(session.session_id);
}
async function loadMessages() {
  const session = state.session, generation = ++state.messageLoad, items = [];
  for (let offset = 0; ; offset += 100) {
    const page = await api("/sessions/" + encodeURIComponent(session) + "/messages?offset=" + offset);
    if (session !== state.session || generation !== state.messageLoad) return;
    items.push(...page.items); if (!page.has_more) break;
  }
  const box = $("messages"); box.replaceChildren();
  if (!items.length) box.append(node("p", "输入需求，开始这次实验。", "empty"));
  for (const item of items) {
    const container = node("article", undefined, "message " + item.role + (item.status === "failed" ? " failed" : ""));
    container.append(node("div", item.role === "user" ? "你" : "实验助手", "role"));
    container.append(node("div", item.content, "body"));
    const meta = item.metadata || {};
    container.dataset.messageId = item.message_id;
    if (meta.response_origin === "application_clarification") container.append(node("div", "需要明确问题范围", "trace"));
    const reportAnswer = item.role === "assistant" && item.task_id && meta.validation?.policy?.startsWith("quality-facts-");
    const retryCodes = ["MODEL_HTTP_ERROR", "MODEL_TIMEOUT", "MODEL_CONNECTION_ERROR", "MODEL_NOT_CONFIGURED", "MODEL_TRUNCATED", "MODEL_INVALID_RESPONSE", "EXPLANATION_PLAN_INVALID"];
    if (reportAnswer && item.status === "failed") {
      if (meta.retryable || retryCodes.includes(meta.error?.code)) {
        const retry = node("button", "重新解释此问题", "secondary retry-explanation");
        retry.disabled = state.sending;
        retry.addEventListener("click", () => retryExplanation(item).catch(error => notice(error.message)));
        container.append(retry);
      }
      const report = node("button", "查看该任务报告", "secondary report-view");
      report.addEventListener("click", async () => {
        try {
          state.task = item.task_id; state.epoch++; resetResult(true);
          $("task-select").value = item.task_id;
          await loadTask(false);
          $("quality").scrollIntoView({ behavior: "smooth", block: "start" });
        } catch (error) { notice(error.message); }
      });
      container.append(report);
    }
    if (meta.request_id?.startsWith("explain:")) state.explained.add(meta.request_id.slice(8));
    const usedModels = [...new Set((meta.model_calls || []).flatMap(c => c.attempts || []).filter(a => a.status === "completed").map(a => a.model + (a.provider === "backup" ? "（备用）" : "")))];
    if (usedModels.length) container.append(node("div", (meta.response_origin === "application_receipt" ? "任务回执 · 请求模型：" : meta.response_origin === "evidence_rendered" ? "报告事实 · 要点选择模型：" : "回答模型：") + usedModels.join("、"), "trace"));
    if (meta.tool_calls?.length || meta.model_calls?.length) {
      const suffix = "调用依据（工具 " + (meta.tool_calls?.length || 0) + " 次）";
      const trace = node("button", "查看" + suffix, "secondary trace");
      const view = node("pre", undefined, "hidden");
      view.id = "call-evidence-" + item.message_id;
      createDisclosure(trace, view, "收起" + suffix, async () => {
        const result = await api("/sessions/" + encodeURIComponent(session) + "/messages/" + encodeURIComponent(item.message_id) + "/calls");
        return JSON.stringify({ validation: meta.validation, tools: result.items.map(call => ({ ...call, requested_by: call.request_key?.startsWith("evidence:") ? "application" : "model" })), models: result.models }, null, 2);
      });
      container.append(trace, view);
    }
    if (item.role === "user" && item.status === "processing") container.append(node("div", "正在读取工具与证据…", "trace"));
    box.append(container);
  }
  const active = state.inflight.has(session) || items.some(item => item.status === "processing");
  sending(active);
  clearTimeout(state.messageTimer);
  if (active) state.messageTimer = setTimeout(() => {
    if (session === state.session) loadMessages().catch(error => { if (session === state.session) notice(error.message); });
  }, 2000);
  box.scrollTop = box.scrollHeight;
  const latest = box.lastElementChild;
  if (latest?.classList.contains("message")) {
    box.scrollTop += latest.getBoundingClientRect().top - box.getBoundingClientRect().top - 20;
  }
}
function sending(active, text = "正在调用模型与工具…") {
  state.sending = active; $("send").disabled = active;
  $("send-state").textContent = active ? text : "Ctrl / ⌘ + Enter 发送";
  document.querySelectorAll("button.retry-explanation").forEach(button => { button.disabled = active; });
  $("request-progress").classList.toggle("hidden", !active);
  $("request-progress-text").textContent = text;
  $("chat-form").setAttribute("aria-busy", String(active));
  $("send").setAttribute("aria-label", active ? "正在发送，请等待回答" : "发送请求");
}
async function retryExplanation(item) {
  if (state.sending) return;
  const session = state.session;
  state.inflight.add(session);
  const key = "movielens-retry-" + session + ":" + item.message_id;
  let payload;
  try { payload = JSON.parse(localStorage.getItem(key)); } catch {}
  if (!payload?.request_id) payload = { request_id: crypto.randomUUID() };
  localStorage.setItem(key, JSON.stringify(payload));
  sending(true, "正在重新解释原问题；任务产物仍可查看…"); notice("");
  try {
    await api("/sessions/" + encodeURIComponent(session) + "/messages/" + encodeURIComponent(item.message_id) + "/retry", {
      method: "POST", body: JSON.stringify(payload)
    });
    localStorage.removeItem(key);
  } catch (error) {
    if (error.body?.request_id === payload.request_id) localStorage.removeItem(key);
    if (session === state.session) notice(error.message);
  } finally {
    state.inflight.delete(session);
    if (session === state.session) {
      sending(false); await loadMessages(); await refreshTasks();
    }
  }
}
function clearSubmittedDraft(session, content) {
  if (state.drafts.get(session) === content) state.drafts.delete(session);
  if (state.session === session && $("prompt").value === content) $("prompt").value = "";
}
async function send(content) {
  if (state.sending || !content.trim()) return;
  sending(true); notice("");
  const session = state.session, selectedTask = state.task;
  state.inflight.add(session);
  const key = "movielens-pending-" + session;
  let previous = null; try { previous = JSON.parse(localStorage.getItem(key)); } catch {}
  const payload = previous?.content === content && previous?.task_id === selectedTask ? previous : {
    request_id: crypto.randomUUID(), content, task_id: selectedTask
  };
  localStorage.setItem(key, JSON.stringify(payload));
  try {
    await api(route() + "/messages", { method: "POST", body: JSON.stringify(payload) });
    localStorage.removeItem(key); clearSubmittedDraft(session, content);
  } catch (error) {
    if (error.body?.request_id === payload.request_id) { localStorage.removeItem(key); clearSubmittedDraft(session, content); }
    if (session === state.session) notice(error.message);
  } finally {
    state.inflight.delete(session);
    if (session === state.session) {
      sending(false);
      await loadMessages(); await refreshTasks();
    }
  }
}
async function refreshTasks(autoExplain = true) {
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
    if (selected !== state.task) { state.task = selected; state.epoch++; resetResult(true); }
    select.value = state.task || "";
    if (state.task) await loadTask(autoExplain);
    if (session !== state.session) return;
    if (page.items.some(t => ["queued", "running"].includes(t.status))) state.timer = setTimeout(() => refreshTasks(autoExplain), 2000);
  } catch (error) {
    if (session === state.session) {
      notice(error.message);
      state.timer = setTimeout(() => refreshTasks(autoExplain), 4000);
    }
  }
}
function stageEntries(task) {
  const attempts = task.attempts || [];
  if (task.workflow !== "governance.v1") return attempts;
  const result = Object.keys(stages).filter(stage => stage !== "published").flatMap(stage => {
    const existing = attempts.filter(attempt => attempt.stage === stage);
    return existing.length ? existing : [{ stage, status: "pending", external_ids: [] }];
  });
  return result.concat(attempts.filter(attempt => !(attempt.stage in stages)));
}
function progressText(metric) {
  const { current, total, unit } = metric;
  if (unit === "percent") return number(current) + "%";
  if (unit === "bytes") {
    const divisor = total >= 1024 * 1024 ? 1024 * 1024 : total >= 1024 ? 1024 : 1;
    const suffix = divisor === 1 ? " B" : divisor === 1024 ? " KiB" : " MiB";
    const format = value => (value / divisor).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
    return format(current) + " / " + format(total) + suffix;
  }
  return number(current) + " / " + number(total) + (unit === "rows" ? " 条" : " 个文件");
}
function renderStages(task) {
  const list = $("attempts"), entries = stageEntries(task);
  if (list.dataset.task !== task.task_id) { list.replaceChildren(); list.dataset.task = task.task_id; }
  $("attempts-panel").classList.toggle("hidden", !entries.length);
  const done = entries.filter(item => item.status === "succeeded").length;
  $("attempts-summary").textContent = "查看执行阶段 · 已完成 " + done + " / " + entries.length;
  $("attempts-note").textContent = ["queued", "running"].includes(task.status)
    ? "每 2 秒刷新；处理量和 Map / Reduce 百分比来自后台实际记录。"
    : "显示最后一次记录；阶段完成数不代表总耗时比例。";
  const keep = new Set();
  for (const [index, attempt] of entries.entries()) {
    const key = attempt.stage + ":" + (attempt.sequence ?? index), title = stages[attempt.stage] || attempt.stage;
    keep.add(key);
    let item = [...list.children].find(child => child.dataset.key === key);
    if (!item) {
      item = node("li", undefined, "stage-item"); item.dataset.key = key; item.dataset.stage = attempt.stage;
      const heading = node("div", undefined, "stage-heading");
      heading.append(node("span", title, "stage-title"), node("span", undefined, "stage-state"));
      item.append(heading, node("div", undefined, "stage-meters"), node("p", undefined, "stage-message"),
        node("p", undefined, "stage-updated"), node("p", undefined, "stage-external footnote"));
      list.insertBefore(item, list.children[index] || null);
    }
    item.dataset.status = attempt.status;
    const status = attempt.status === "pending" ? (["failed", "unknown"].includes(task.status) ? "未执行" : "等待中") : statuses[attempt.status] || attempt.status;
    item.querySelector(".stage-state").textContent = status;
    const progress = attempt.progress;
    let message = progress?.message || "";
    if (attempt.status === "succeeded") message = progress ? "阶段完成" : "阶段已完成；历史任务未记录处理中计数。";
    else if (attempt.status === "pending") message = task.status === "queued" ? "等待后台开始执行" : ["failed", "unknown"].includes(task.status) ? "前序任务停止，后续阶段未执行" : "等待前序阶段完成";
    else if (attempt.status === "failed") message = "本阶段失败，进度停留在最后一次记录";
    else if (attempt.status === "unknown") message = "执行状态待核查，进度停留在最后一次记录";
    else if (!message) message = "正在执行，尚无可计量的进度";
    item.querySelector(".stage-message").textContent = message;
    const updated = progress?.updated_at;
    item.querySelector(".stage-updated").textContent = updated ? "最近进度 " + new Date(updated).toLocaleTimeString("zh-CN") : "";
    item.querySelector(".stage-external").textContent = (attempt.external_ids || []).join(" / ");
    const meters = item.querySelector(".stage-meters");
    const metrics = progress?.metrics?.length ? progress.metrics : [{ key: "state", label: status, fallback: true }];
    const metricKeys = new Set(metrics.map(metric => metric.key));
    for (const old of [...meters.children]) if (!metricKeys.has(old.dataset.key)) old.remove();
    for (const metric of metrics) {
      let row = [...meters.children].find(child => child.dataset.key === metric.key);
      if (!row) {
        row = node("div", undefined, "stage-meter"); row.dataset.key = metric.key;
        const label = node("div", undefined, "stage-meter-label");
        label.append(node("span", undefined, "meter-title"), node("span", undefined, "meter-value"));
        const bar = node("progress", undefined, "stage-progress");
        row.append(label, bar); meters.append(row);
      }
      const bar = row.querySelector("progress"), text = metric.fallback ? "" : progressText(metric);
      row.querySelector(".meter-title").textContent = metric.label;
      row.querySelector(".meter-value").textContent = text;
      bar.setAttribute("aria-label", title + " · " + metric.label);
      if (metric.fallback) {
        bar.max = 1;
        if (attempt.status === "running") { bar.removeAttribute("value"); bar.setAttribute("aria-valuetext", "进度待上报"); }
        else { bar.value = attempt.status === "succeeded" ? 1 : 0; bar.setAttribute("aria-valuetext", status); }
      } else {
        bar.max = Math.max(1, metric.total); bar.value = metric.total === 0 ? 1 : metric.current;
        bar.setAttribute("aria-valuetext", text);
      }
    }
  }
  for (const old of [...list.children]) if (!keep.has(old.dataset.key)) old.remove();
}
async function loadTask(autoExplain = true) {
  const epoch = state.epoch, taskId = state.task;
  const task = await api(route() + "/tasks/" + taskId);
  if (epoch !== state.epoch || taskId !== state.task) return;
  state.detail = task;
  $("task-status").replaceChildren(node("span", statuses[task.status], "badge " + task.status), node("span", stages[task.stage] || "等待后台 worker 认领"));
  $("task-error").textContent = task.error || ""; $("task-error").classList.toggle("hidden", !task.error);
  renderStages(task);
  if (task.status !== "succeeded") { resetResult(); state.detail = task; return; }
  if (state.qualityTask !== taskId) {
    const result = await api(route() + "/tasks/" + taskId + "/quality");
    if (epoch !== state.epoch || taskId !== state.task) return;
    renderQuality(result.data.value, task); state.qualityTask = taskId;
  }
  if (autoExplain && !state.sending && !state.explained.has(taskId)) explain(taskId).catch(error => notice(error.message));
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
  sampleDisclosure.reset(); exampleDisclosure.reset();
  $("quality-empty").classList.add("hidden"); $("quality").classList.remove("hidden"); $("metrics").replaceChildren();
  $("dispositions").replaceChildren();
  for (const table of ["users", "movies", "ratings"]) {
    const d = value.after.dispositions[table], row = node("tr");
    const values = [labels[table], number(value.before.tables[table].rows), number(value.after.tables[table].rows), number(d.repaired || 0), number(d.deduplicated || 0), number(d.quarantined || 0)];
    values.forEach(v => row.append(node("td", v))); $("dispositions").append(row);
  }
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
    const link = node("a");
    const icon = node("span", "↓", "file-icon"); icon.setAttribute("aria-hidden", "true");
    const size = node("span", fileSize(file.size_bytes), "file-size"); size.setAttribute("aria-hidden", "true");
    link.append(icon, node("span", file.name, "file-name"), size);
    link.href = "/api/v1" + artifactRoute(artifact.ref) + "/download?file_name=" + encodeURIComponent(file.name);
    link.setAttribute("download", file.name); $("downloads").append(link);
  }
  $("versions").textContent = JSON.stringify({ task_id: task.task_id, input: value.input_ref, cleaned: value.cleaned_ref, configuration: value.config_refs }, null, 2);
  $("issue-rule").replaceChildren();
  for (const table of ["users", "movies", "ratings"]) for (const reason of Object.keys(value.after.reasons[table])) {
    $("issue-rule").append(new Option(labels[table] + " · " + reason, table + "/" + reason));
  }
  renderWorkspace(value, task);
}
function renderWorkspace(value, task) {
  sectionLinks[1].href = "#quality-area";
  sectionLinks[2].href = "#evidence-area";
  const input = value.before.tables.ratings.rows;
  const retained = value.after.tables.ratings.rows;
  const quarantined = value.after.dispositions.ratings.quarantined || 0;
  const files = task.artifacts.flatMap(artifact => artifact.files);
  $("summary-retained").textContent = number(retained);
  $("summary-input").textContent = "原始 " + number(input) + " 条";
  $("summary-quarantined").textContent = number(quarantined);
  $("summary-loss").textContent = input > 0 ? "占原始评分的 " + (quarantined / input * 100).toFixed(2) + "%" : "原始评分为空";
  $("summary-files").textContent = number(files.length);
  $("summary-evidence").textContent = "数据、报告与来源";
  $("overview-state").textContent = "来自当前已发布任务";
  $("context-caption").textContent = "围绕任务 " + task.task_id.slice(0, 8) + " 继续追问";

  $("metrics").replaceChildren();
  for (const [key, label] of Object.entries(dimensions)) {
    const before = value.before.overall[key].score, after = value.after.overall[key].score;
    const metric = node("article", undefined, "metric");
    metric.dataset.dimension = key;
    const heading = node("div", undefined, "metric-heading");
    heading.append(node("h3", label), node("span", key));
    const result = node("div", undefined, "metric-value");
    result.append(node("strong", score(after)), node("span", "分"));
    const previous = node("div", "清洗前 " + score(before), "metric-history");
    const bars = node("div", undefined, "metric-bars");
    for (const [v, kind] of [[before, "before"], [after, "after"]]) {
      const bar = node("progress", undefined, "bar " + kind);
      bar.max = 100; bar.value = Number.isFinite(v) ? v : 0;
      bar.setAttribute("aria-label", label + (kind === "before" ? "清洗前 " : "清洗后 ") + score(v));
      if (!Number.isFinite(v)) bar.setAttribute("aria-valuetext", "不可评价");
      bars.append(bar);
    }
    const delta = Number.isFinite(before) && Number.isFinite(after) ? after - before : null;
    const deltaText = delta === null ? "变化不可评价" : delta === 0 ? "持平" : (delta > 0 ? "↑ " : "↓ ") + Math.abs(delta).toFixed(3);
    const change = node("span", deltaText, "metric-delta" + (delta !== null && delta < 0 ? " decline" : ""));
    change.setAttribute("aria-label", delta === null ? "变化不可评价" : "变化 " + (delta > 0 ? "+" : "") + delta.toFixed(3) + " 分");
    metric.append(heading, result, bars, previous, change);
    $("metrics").append(metric);
  }
  const guide = node("aside", undefined, "metric-guide");
  guide.append(node("span", "读懂这些分数"), node("p", "满分代表通过已实现的约束。历史数据的时效性需要单独看待。"));
  $("metrics").append(guide);

  renderSplits(value);
}
function fileSize(bytes) {
  if (!Number.isFinite(bytes)) return "下载";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}
function renderSplits(value) {
  const names = { train: "训练集", validation: "验证集", test: "测试集" };
  const splits = value.after.splits;
  const total = Object.keys(names).reduce((sum, key) => sum + (splits[key] || 0), 0);
  const box = $("splits"); box.replaceChildren();
  const heading = node("div", undefined, "split-heading");
  heading.append(node("h3", "按时间划分"), node("span", "仅统计范围，未另存分区文件"));
  box.append(heading);
  const chart = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  chart.setAttribute("viewBox", "0 0 1000 12"); chart.setAttribute("preserveAspectRatio", "none");
  chart.setAttribute("class", "split-bar"); chart.setAttribute("role", "img");
  chart.setAttribute("aria-label", Object.entries(names).map(([key, name]) => name + " " + number(splits[key]) + " 条").join("，"));
  let x = 0;
  for (const key of Object.keys(names)) {
    const width = total ? splits[key] / total * 1000 : 0;
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    for (const [name, val] of Object.entries({ x, y: 0, width, height: 12, class: "split-" + key })) rect.setAttribute(name, val);
    chart.append(rect); x += width;
  }
  const legend = node("div", undefined, "split-legend");
  for (const [key, name] of Object.entries(names)) {
    const item = node("div", undefined, "split-item " + key);
    item.append(node("span", name), node("strong", number(splits[key])));
    legend.append(item);
  }
  const boundaries = node("p", "T1 " + utc(value.configuration.split.train_end) + "；T2 " + utc(value.configuration.split.validation_end), "split-boundaries");
  box.append(chart, legend, boundaries);
}
sectionLinks.forEach(link => link.addEventListener("click", () => {
  sectionLinks.forEach(item => item.removeAttribute("aria-current"));
  link.setAttribute("aria-current", "location");
}));
$("chat-form").addEventListener("submit", event => { event.preventDefault(); send($("prompt").value).catch(error => notice(error.message)); });
$("prompt").addEventListener("keydown", event => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); $("chat-form").requestSubmit(); } });
$("suggest-run").addEventListener("click", () => { $("prompt").value = "请使用默认规则清洗 MovieLens 1M，评估前后五维质量，并说明处理的问题与局限。"; $("prompt").focus(); });
$("suggest-explain").addEventListener("click", () => { $("prompt").value = "请读取当前任务的实际指标，解释分数变化和数据处置，并说明哪些问题仍无法核实。"; $("prompt").focus(); });
$("new-session").addEventListener("click", () => newSession().catch(error => notice(error.message)));
$("refresh").addEventListener("click", () => { state.qualityTask = null; refreshTasks(false); loadMessages().catch(error => notice(error.message)); });
$("task-select").addEventListener("change", () => { state.task = $("task-select").value || null; state.epoch++; resetResult(true); if (state.task) loadTask(false).catch(error => notice(error.message)); });
const sampleDisclosure = createDisclosure($("load-sample"), $("sample"), "收起样例", async () => {
  const artifact = state.detail.artifacts.find(item => item.kind === "cleaned_dataset");
  const value = await api(artifactRoute(artifact.ref) + "?mode=sample&limit=3&file_name=" + $("sample-table").value + ".jsonl");
  return JSON.stringify(value.data.value, null, 2);
});
const exampleDisclosure = createDisclosure($("load-example"), $("examples"), "收起依据", async () => {
  const artifact = state.detail.artifacts.find(item => item.kind === "quality_report");
  const value = await api(artifactRoute(artifact.ref) + "?mode=examples&limit=3&reason=" + encodeURIComponent($("issue-rule").value));
  return JSON.stringify(value.data.value, null, 2);
});
$("sample-table").addEventListener("change", sampleDisclosure.reset);
$("issue-rule").addEventListener("change", exampleDisclosure.reset);
function updateModelState(status) {
  const provider = { auto: "自动主备", primary: "主服务", backup: "备用服务" }[status.model_provider];
  $("model-state").textContent = status.model_configured ? (status.model_name || status.backup_model_name || "已配置模型") + " · " + provider : "模型尚未配置";
}
(async () => {
  const status = await api("/status");
  updateModelState(status);
  const requested = new URLSearchParams(location.search).get("session");
  const previous = requested || localStorage.getItem("movielens-session");
  if (previous) {
    try { await openSession(previous); return; }
    catch (error) { if (error.status !== 404) throw error; }
  }
  await newSession();
})().catch(error => notice(error.message));
