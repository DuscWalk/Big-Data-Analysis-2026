"use strict";
(() => {
  const dialog = $("governance-dialog"), form = $("governance-form");
  const tables = ["users", "movies", "ratings"];
  let schemes = [], defaultState = null, source = null, seed = null, generation = 0, loading = 0, busy = false;
  const feedback = (text = "", tone = "") => { $("governance-feedback").textContent = text; $("governance-feedback").dataset.tone = tone; };
  const dateValue = stamp => new Date(stamp * 1000).toISOString().slice(0, 19);
  const readTime = id => Math.round(new Date($(id).value + "Z").getTime() / 1000);
  const selected = () => schemes.find(item => item.ref.version === (state.configurationRef || defaultState?.ref)?.version);
  const key = () => "movielens-governance-" + state.session;

  function renderSelection() {
    const select = $("governance-select"), current = selected();
    const defaultName = schemes.find(item => item.ref.version === defaultState?.ref.version)?.name || "正在读取";
    select.replaceChildren(new Option("默认 · " + defaultName, ""), ...schemes.map(item => new Option(item.name + " · " + item.ref.version.slice(7, 15), item.ref.version)));
    if (state.configurationRef && !current) select.append(new Option("所选方案不可用，请重新选择", state.configurationRef.version));
    select.value = state.configurationRef?.version || "";
    select.disabled = !defaultState;
    $("governance-selection-note").textContent = current
      ? "仅用于新清洗任务；查看历史结果沿用该任务配置。可在对话中描述修改或要求设为默认。"
      : state.configurationRef ? "未找到所选方案；发送清洗请求前请重新选择，不会自动改用默认方案。" : "正在读取默认方案…";
  }
  function selectScheme(ref) {
    state.configurationRef = ref || null;
    state.configurationSelection++;
    if (state.session) {
      if (ref) localStorage.setItem(key(), JSON.stringify(ref));
      else localStorage.removeItem(key());
    }
    renderSelection();
  }
  async function refresh() {
    const current = ++loading, items = [];
    let page;
    for (let offset = 0; ; offset += 100) {
      page = await api("/governance-configs?offset=" + offset + "&limit=100");
      if (current !== loading) return;
      items.push(...page.items);
      if (!page.has_more) break;
    }
    schemes = items; defaultState = page.default;
    renderSelection();
    renderSaved();
  }
  function renderSaved() {
    const saved = $("governance-saved"), previous = saved.value;
    saved.replaceChildren(...schemes.map(item => new Option(item.name + (item.ref.version === defaultState.ref.version ? " · 默认" : "") + " · " + item.ref.version.slice(7, 15), item.ref.version)));
    if (schemes.some(item => item.ref.version === previous)) saved.value = previous;
    const name = schemes.find(item => item.ref.version === defaultState.ref.version)?.name || "已登记方案";
    $("governance-default-caption").textContent = "当前默认：" + name + "。方案按内容版本保存；相同内容会复用已有方案。";
  }
  function locks() {
    $("governance-reference").disabled = $("governance-reference-linked").checked;
    if ($("governance-reference-linked").checked) $("governance-reference").value = $("governance-max").value;
    for (const table of tables) $("governance-weight-" + table).disabled = $("governance-equal-weights").checked;
  }
  function draft() {
    const windowSeconds = Math.round(Number($("governance-window").value) * 86400);
    const weights = Object.fromEntries(tables.map(table => [table, $("governance-equal-weights").checked ? 1 : Number($("governance-weight-" + table).value)]));
    return {
      schema_version: "1",
      rules: { version: "rules-v2", policy: "valid-first-isolate-conflicts", timestamp_min: readTime("governance-min"), timestamp_max: readTime("governance-max"),
        quarantine_zip_warnings: $("governance-zip").checked, quarantine_title_warnings: $("governance-title").checked, quarantine_encoding_warnings: $("governance-encoding").checked },
      metrics: { version: "metrics-v2", formula: "constraint-ratios-weighted-tables", reference_time: readTime("governance-reference"), window_seconds: windowSeconds, table_weights: weights },
      split: { version: "split-v1", train_end: readTime("governance-train"), validation_end: readTime("governance-validation") },
    };
  }
  function fill(item) {
    source = item;
    $("governance-saved").value = item.ref.version;
    $("governance-name").value = item.name;
    const c = item.configuration, weights = c.metrics.table_weights || { users: 1, movies: 1, ratings: 1 };
    for (const [id, value] of [["min", c.rules.timestamp_min], ["max", c.rules.timestamp_max], ["reference", c.metrics.reference_time], ["train", c.split.train_end], ["validation", c.split.validation_end]]) $("governance-" + id).value = dateValue(value);
    for (const [id, flag] of [["zip", "quarantine_zip_warnings"], ["title", "quarantine_title_warnings"], ["encoding", "quarantine_encoding_warnings"]]) $("governance-" + id).checked = Boolean(c.rules[flag]);
    $("governance-reference-linked").checked = c.metrics.reference_time === c.rules.timestamp_max;
    $("governance-window").value = c.metrics.window_seconds / 86400;
    $("governance-equal-weights").checked = weights.users === weights.movies && weights.movies === weights.ratings;
    for (const table of tables) $("governance-weight-" + table).value = weights[table];
    locks(); seed = JSON.stringify(draft());
  }
  async function open() {
    const current = ++generation;
    busy = false; source = null; $("governance-fields").disabled = true;
    feedback("正在读取方案…"); dialog.showModal();
    try {
      await refresh();
      if (current !== generation) return;
      const item = selected() || schemes.find(value => value.ref.version === defaultState.ref.version);
      fill(item); $("governance-fields").disabled = false; locks(); feedback();
    } catch (error) { if (current === generation) feedback(error.message + " 请关闭后重试。", "error"); }
  }
  async function save(makeDefault) {
    if (busy || !source || !form.reportValidity()) return;
    const c = draft(), r = c.rules, s = c.split, m = c.metrics;
    if (!(r.timestamp_min <= s.train_end && s.train_end < s.validation_end && s.validation_end < r.timestamp_max)) {
      feedback("未保存：请满足允许下界 ≤ T1 < T2 < 允许上界。", "error"); return;
    }
    if (!Number.isInteger(m.window_seconds) || m.window_seconds <= 0 || m.window_seconds > m.reference_time) {
      feedback("未保存：时效窗口须至少 1 秒，窗口起点不能早于 1970 年。", "error"); return;
    }
    const current = generation, session = state.session, selection = state.configurationSelection;
    // Preserve the exact legacy version when saving an unchanged form.
    const configuration = JSON.stringify(c) === seed ? source.configuration : c;
    const body = { name: $("governance-name").value.trim(), configuration, set_default: makeDefault, revision: defaultState.revision };
    busy = true; $("governance-fields").disabled = true;
    feedback(makeDefault ? "正在保存并设置默认方案…" : "正在保存方案…");
    let item = null;
    try {
      item = await api("/governance-configs", { method: "POST", body: JSON.stringify(body) });
      schemes = [item, ...schemes.filter(value => value.ref.version !== item.ref.version)];
      defaultState = item.default;
      renderSelection(); renderSaved();
      if (session === state.session && selection === state.configurationSelection) selectScheme(makeDefault ? null : item.ref);
      await refresh();
      if (current !== generation) return;
      fill(item);
      feedback(makeDefault ? "已设为默认，之后未指定方案的新请求将使用此配置。" : "已保存并选用。发送清洗请求时使用此方案；尚未提交计算。", "success");
    } catch (error) {
      if (current !== generation) return;
      if (item) {
        fill(item);
        feedback((makeDefault ? "已保存并设为默认" : "已保存方案") + "，但列表刷新失败。可关闭后重新打开查看。", "error");
      } else feedback("未保存：" + error.message, "error");
      // Refresh only the revision/list; preserve the user's draft on conflict.
      if (error.status === 409) await refresh().catch(() => {});
    } finally { if (current === generation) { busy = false; $("governance-fields").disabled = false; locks(); } }
  }
  $("open-governance-settings").addEventListener("click", open);
  $("close-governance-settings").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { generation++; });
  form.addEventListener("submit", event => { event.preventDefault(); save(false); });
  $("governance-set-default").addEventListener("click", () => save(true));
  $("governance-saved").addEventListener("change", () => { const item = schemes.find(value => value.ref.version === $("governance-saved").value); if (item) { fill(item); feedback("已载入，可修改后保存或直接设为默认。"); } });
  for (const id of ["governance-reference-linked", "governance-equal-weights", "governance-max"]) $(id).addEventListener("input", locks);
  $("governance-select").addEventListener("change", () => selectScheme(schemes.find(item => item.ref.version === $("governance-select").value)?.ref));
  document.addEventListener("movielens-session", renderSelection);
  window.governanceSettings = {
    async acceptChange(change, session, selection) {
      await refresh();
      if (change && session === state.session && selection === state.configurationSelection) selectScheme(change.default_changed ? null : change.ref);
    },
  };
  refresh().catch(error => { $("governance-selection-note").textContent = "方案读取失败，可打开治理配置重试：" + error.message; });
})();
