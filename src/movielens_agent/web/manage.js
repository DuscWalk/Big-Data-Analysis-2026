"use strict";
(() => {
  const modelDialog = $("model-dialog"), historyDialog = $("history-dialog");
  let configuration = null, modelGeneration = 0, modelBusy = false;
  let historyGeneration = 0, historyOffset = 0, historyQuery = "";
  const feedback = (id, text = "", tone = "") => { $(id).textContent = text; $(id).dataset.tone = tone; };
  const clearKeys = () => { for (const provider of ["primary", "backup"]) $(provider + "-key").value = ""; };
  document.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => $(button.dataset.close).close()));
  modelDialog.addEventListener("close", () => { modelGeneration++; modelBusy = false; clearKeys(); configuration = null; });
  historyDialog.addEventListener("close", () => { historyGeneration++; });

  function populate(value) {
    configuration = value;
    $("model-provider").value = value.provider;
    $("model-timeout").value = value.timeout_seconds;
    $("model-max-tokens").value = value.max_tokens;
    for (const provider of ["primary", "backup"]) {
      const item = value[provider];
      $(provider + "-url").value = item.url;
      $(provider + "-model").value = item.model;
      $(provider + "-key").value = "";
      $(provider + "-key").disabled = false;
      $(provider + "-key").placeholder = item.key_configured ? "已保存，留空沿用" : "尚未配置";
      $(provider + "-clear-key").checked = false;
      $(provider + "-state").textContent = item.configured ? "已配置 · 可检测" : "配置待补全";
      $(provider + "-model-options").classList.add("hidden");
      $(provider + "-model-list").replaceChildren(new Option("请选择", ""));
      feedback(provider + "-feedback");
    }
  }
  async function openModels() {
    const generation = ++modelGeneration;
    modelBusy = false; configuration = null; clearKeys();
    $("model-form").reset(); $("model-fields").disabled = true;
    for (const provider of ["primary", "backup"]) { feedback(provider + "-feedback"); $(provider + "-model-options").classList.add("hidden"); }
    feedback("model-feedback", "正在读取当前设置…");
    modelDialog.showModal();
    try {
      const value = await api("/model-settings");
      if (generation !== modelGeneration) return;
      populate(value);
      feedback("model-feedback", value.saved ? "当前使用已保存的本地设置。" : "当前使用启动配置，可在这里修改并保存。");
      $("model-fields").disabled = false;
    } catch (error) { if (generation === modelGeneration) feedback("model-feedback", error.message + " 请关闭后重试。", "error"); }
  }
  $("open-model-settings").addEventListener("click", openModels);
  $("open-model-settings-inline").addEventListener("click", openModels);
  function draft() {
    const value = { revision: configuration.revision, provider: $("model-provider").value,
      timeout_seconds: Number($("model-timeout").value), max_tokens: Number($("model-max-tokens").value) };
    for (const provider of ["primary", "backup"]) value[provider] = {
      url: $(provider + "-url").value.trim(), model: $(provider + "-model").value.trim(),
      api_key: $(provider + "-key").value || null, clear_key: $(provider + "-clear-key").checked,
    };
    return value;
  }
  $("model-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (modelBusy || !configuration || !$("model-form").reportValidity()) return;
    const generation = modelGeneration, body = draft();
    modelBusy = true; $("model-fields").disabled = true;
    feedback("model-feedback", "正在保存设置…");
    try {
      const result = await api("/model-settings", { method: "PUT", body: JSON.stringify(body) });
      updateModelState({ model_configured: result.provider === "auto" ? result.primary.configured || result.backup.configured : result[result.provider].configured,
        model_name: result.provider === "backup" ? result.backup.model : result.primary.model,
        model_provider: result.provider, backup_model_name: result.backup.configured ? result.backup.model : null });
      if (generation !== modelGeneration) return;
      populate(result);
      feedback("model-feedback", "已保存，新请求将使用这份设置。", "success");
    } catch (error) { if (generation === modelGeneration) feedback("model-feedback", error.message, "error"); }
    finally { if (generation === modelGeneration) { modelBusy = false; $("model-fields").disabled = false; } }
  });
  const checkErrors = {
    MODEL_NOT_CONFIGURED: "请补全服务地址、模型名称与密钥。", MODEL_TIMEOUT: "请求超时，请检查服务状态或调整超时。",
    MODEL_CONNECTION_ERROR: "无法连接服务，请检查地址与网络。", INVALID_PROBE_CALL: "服务已响应，但没有返回检测要求的工具调用。",
    MODEL_INVALID_RESPONSE: "响应不符合模型调用协议。", MODEL_TRUNCATED: "响应被截断，可调整输出上限后再试。",
  };
  async function checkModel(provider, kind) {
    if (modelBusy || !configuration || !$("model-form").reportValidity()) return;
    const generation = modelGeneration, body = { provider, configuration: draft() };
    modelBusy = true; $("model-fields").disabled = true;
    feedback(provider + "-feedback", kind === "models" ? "正在获取模型列表…" : "正在检测连接与工具调用，请稍候…");
    try {
      const result = await api("/model-settings/" + kind, { method: "POST", body: JSON.stringify(body) });
      if (generation !== modelGeneration) return;
      if (kind === "models") {
        const item = result.providers[0];
        if (item.error || item.http_status !== 200 || !Array.isArray(item.models)) {
          throw new Error(item.error === "MODEL_NOT_CONFIGURED" ? "请先填写服务地址和密钥。" : "无法获取模型列表" + (item.http_status ? "（HTTP " + item.http_status + "）" : "") + "，可手动填写模型名称。");
        }
        const choices = [...new Set(item.models)].sort().slice(0, 2000), select = $(provider + "-model-list");
        select.replaceChildren(new Option("请选择", ""), ...choices.map(name => new Option(name, name)));
        if (choices.includes($(provider + "-model").value)) select.value = $(provider + "-model").value;
        $(provider + "-model-options").classList.toggle("hidden", !choices.length);
        feedback(provider + "-feedback", choices.length ? "已读取 " + choices.length + " 个模型，可选择后检测。" : "服务返回空列表，可手动填写模型名称。", choices.length ? "success" : "");
      } else {
        const timing = "用时 " + (result.duration_ms / 1000).toFixed(1) + " 秒 · " + new Date(result.checked_at).toLocaleTimeString("zh-CN");
        const passed = result.status === "completed" && result.native_tool_call;
        feedback(provider + "-feedback", (passed ? "连接正常，原生工具调用通过。" : "检测未通过：" + (checkErrors[result.error] || result.message || result.error || "请检查模型名称和服务支持情况。")) + "\n" + timing, passed ? "success" : "error");
      }
    } catch (error) { if (generation === modelGeneration) feedback(provider + "-feedback", error.message, "error"); }
    finally { if (generation === modelGeneration) { modelBusy = false; $("model-fields").disabled = false; } }
  }
  for (const provider of ["primary", "backup"]) {
    $(provider + "-models").addEventListener("click", () => checkModel(provider, "models"));
    $(provider + "-probe").addEventListener("click", () => checkModel(provider, "probe"));
    $(provider + "-model-list").addEventListener("change", () => {
      if ($(provider + "-model-list").value) $(provider + "-model").value = $(provider + "-model-list").value;
      feedback(provider + "-feedback", "模型名称已填入表单，检测通过后可保存。");
    });
    $(provider + "-clear-key").addEventListener("change", () => {
      $(provider + "-key").value = ""; $(provider + "-key").disabled = $(provider + "-clear-key").checked;
      feedback(provider + "-feedback"); $(provider + "-model-options").classList.add("hidden");
    });
    for (const suffix of ["url", "model", "key"]) $(provider + "-" + suffix).addEventListener("input", () => {
      feedback(provider + "-feedback");
      if (suffix !== "model") $(provider + "-model-options").classList.add("hidden");
    });
  }

  async function loadHistory(reset = false) {
    if (reset) {
      historyGeneration++; historyOffset = 0; historyQuery = $("history-query").value.trim();
      $("history-list").replaceChildren(); $("history-more").classList.add("hidden");
    }
    const generation = historyGeneration, offset = historyOffset;
    $("history-more").disabled = true; feedback("history-feedback", "正在读取会话…");
    try {
      const result = await api("/sessions?q=" + encodeURIComponent(historyQuery) + "&offset=" + offset + "&limit=20");
      if (generation !== historyGeneration) return;
      for (const item of result.items) {
        const button = node("button", undefined, "history-item"); button.type = "button"; button.dataset.sessionId = item.session_id;
        button.setAttribute("aria-current", String(item.session_id === state.session));
        const title = node("span", undefined, "history-title"); title.append(node("span", item.title));
        if (item.session_id === state.session) title.append(node("strong", "当前会话"));
        button.append(title, node("span", item.preview || "尚无提问", "history-preview"),
          node("span", new Date(item.updated_at).toLocaleString("zh-CN") + " · " + item.message_count + " 条消息 · " + item.task_count + " 个任务" + (item.processing ? " · 正在回答" : "") + " · " + item.session_id.slice(0, 12), "history-meta"));
        button.addEventListener("click", async () => {
          const opening = ++historyGeneration;
          $("history-list").querySelectorAll("button").forEach(entry => { entry.disabled = true; });
          feedback("history-feedback", "正在打开会话…");
          try { await openSession(item.session_id); if (opening === historyGeneration) historyDialog.close(); }
          catch (error) { if (opening === historyGeneration) feedback("history-feedback", error.message, "error"); }
          finally { if (opening === historyGeneration) $("history-list").querySelectorAll("button").forEach(entry => { entry.disabled = false; }); }
        });
        $("history-list").append(button);
      }
      historyOffset += result.items.length;
      $("history-more").classList.toggle("hidden", !result.has_more);
      feedback("history-feedback", historyOffset ? "已显示 " + historyOffset + " 个会话。" : historyQuery ? "没有找到匹配的会话。" : "还没有历史会话。");
    } catch (error) { if (generation === historyGeneration) feedback("history-feedback", error.message + " 可点击查找重试。", "error"); }
    finally { if (generation === historyGeneration) $("history-more").disabled = false; }
  }
  $("open-history").addEventListener("click", () => { $("history-query").value = ""; historyDialog.showModal(); loadHistory(true); });
  $("history-search").addEventListener("submit", event => { event.preventDefault(); loadHistory(true); });
  $("history-more").addEventListener("click", () => loadHistory());
})();
