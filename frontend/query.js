"use strict";

const CHAIN_BUILD_ID = "20260802.3"; // compatibility: 20260729.3
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const escapeHTML = (value) => String(value ?? "").replace(/[&<>'"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" }[ch]));
let polling = false;
let runtimeConfig = { checked: false, ready: false, missing: [], error: "" };

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", "X-Chain-Risk-Build": CHAIN_BUILD_ID, ...(options.headers || {}) },
    ...options,
  });
  let data = {};
  try { data = await response.json(); } catch (_) { data = {}; }
  if (!response.ok) {
    const error = new Error(data.detail || `请求失败（${response.status}）`);
    error.code = data.code || `HTTP_${response.status}`;
    throw error;
  }
  return data;
}

const landing = $("#landing");
const dashboard = $("#dashboard");
const heroVideo = $("#heroVideo");
let revealed = false;
function revealLanding() {
  if (revealed) return;
  revealed = true;
  landing?.classList.add("is-revealed");
}
landing?.addEventListener("pointermove", revealLanding, { once: true });
landing?.addEventListener("touchstart", revealLanding, { once: true, passive: true });
landing?.addEventListener("keydown", revealLanding, { once: true });
heroVideo?.addEventListener("error", () => landing?.classList.add("video-missing"));
setTimeout(() => { if (heroVideo && heroVideo.readyState < 2) landing?.classList.add("video-missing"); }, 1800);
$("#startButton")?.addEventListener("click", () => {
  if (landing) landing.hidden = true;
  if (dashboard) dashboard.hidden = false;
  window.scrollTo({ top: 0, behavior: "auto" });
  $("#companyQuery")?.focus();
});

function openQueryWorkspace({ restoreQuery = false } = {}) {
  if (landing) landing.hidden = true;
  if (dashboard) dashboard.hidden = false;
  const field = $("#companyQuery");
  if (field) field.value = "";
  requestAnimationFrame(() => {
    window.scrollTo({ top: 0, behavior: "auto" });
    field?.focus();
  });
}

function showQueryError(message, code = "") {
  const dialog = $("#errorDialog");
  $("#errorDialogMessage").textContent = message;
  $("#errorDialogCode").textContent = code ? `错误代码：${code}` : "";
  if (typeof dialog?.showModal === "function") dialog.showModal();
  else dialog?.setAttribute("open", "");
}
$("#errorDialogClose")?.addEventListener("click", () => {
  const dialog = $("#errorDialog");
  if (typeof dialog?.close === "function") dialog.close();
  else dialog?.removeAttribute("open");
  $("#companyQuery")?.focus();
});

async function refreshConfigStatus() {
  const status = $("#serviceStatus");
  const submit = $("#searchForm button[type='submit']");
  try {
    const config = await fetchJSON("/api/config");
    $("#modelLabel").textContent = config.model_label || "DeepSeek V4 + Tavily";
    const missing = [];
    if (!config.search_ready) missing.push("Tavily API");
    if (!config.deepseek_ready) missing.push("DeepSeek API");
    runtimeConfig = { checked: true, ready: missing.length === 0, missing, error: "" };
    if (missing.length) {
      status.className = "service-status is-error";
      status.innerHTML = `<span>未配置：${escapeHTML(missing.join("、"))}。按钮仍可点击并查看配置提示；填写项目根目录 .env 后请重启服务。</span>`;
      $("#modelLabel").textContent = "API待配置";
      submit.dataset.configReady = "false";
      submit.title = "点击查看 API 配置提示";
    } else {
      status.className = "service-status is-ready";
      status.innerHTML = "<span>实时检索与风险分析服务已就绪</span>";
      submit.dataset.configReady = "true";
      submit.title = "开始企业风险分析";
    }
    // 缺少密钥时也不能把主按钮做成失效控件；点击后给出明确配置提示。
    submit.disabled = false;
    return runtimeConfig;
  } catch (error) {
    $("#modelLabel").textContent = "服务状态未知";
    status.className = "service-status is-error";
    status.innerHTML = "<span>无法读取后端配置。按钮仍可点击并查看故障提示，请确认服务已启动。</span>";
    runtimeConfig = { checked: true, ready: false, missing: [], error: error?.message || "无法读取后端配置" };
    submit.dataset.configReady = "false";
    submit.title = "点击查看服务状态提示";
    submit.disabled = false;
    return runtimeConfig;
  }
}

function updateProgress(task) {
  const progress = Math.max(0, Math.min(100, Number(task.progress || 0)));
  $("#progressBar").style.width = `${progress}%`;
  $("#progressValue").textContent = `${progress}%`;
  $("#loadingText").textContent = task.message || task.stage || "正在处理";
  const stageMap = { "启动": 0, "缓存检查": 0, "证券主体解析": 0, "主体核验": 0, "主体锁定": 0, "主题并行检索": 1, "证据精读": 1, "规则解析": 2, "并行结构化": 2, "精准补查": 3, "证据补全": 3, "风险计算": 4, "决策分析": 4, "完成": 4 };
  const active = stageMap[task.stage] ?? 0;
  $$(".loading-steps div").forEach((step, index) => {
    step.classList.toggle("active", index === active && task.status !== "completed");
    step.classList.toggle("done", index < active || task.status === "completed");
  });
}

function goToReport(reportId) {
  document.body.classList.add("page-leaving");
  sessionStorage.setItem("chain-last-report", reportId);
  setTimeout(() => { window.location.href = `/report?report_id=${encodeURIComponent(reportId)}`; }, 430);
}

async function runAnalysis(query) {
  const loadingPanel = $("#loadingPanel");
  loadingPanel.hidden = false;
  document.body.classList.add("is-researching");
  polling = true;
  updateProgress({ progress: 0, stage: "启动", message: "正在创建实时尽调任务" });
  requestAnimationFrame(() => loadingPanel.scrollIntoView({ behavior: "smooth", block: "nearest" }));
  try {
    const validation = await fetchJSON("/api/validate-query", { method: "POST", body: JSON.stringify({ query, force_refresh: false }) });
    $("#companyQuery").value = validation.normalized || query;
    const task = await fetchJSON("/api/research", { method: "POST", body: JSON.stringify({ query: validation.normalized || query, force_refresh: false }) });
    while (polling) {
      const status = await fetchJSON(`/api/research/${encodeURIComponent(task.task_id)}`);
      updateProgress(status);
      if (status.status === "failed") {
        const error = new Error(status.error || "尽调任务未完成");
        error.code = status.error_code || "RESEARCH_FAILED";
        throw error;
      }
      if (status.status === "completed") {
        polling = false;
        updateProgress({ ...status, progress: 100, stage: "完成", message: "完整报告已生成，正在打开" });
        goToReport(status.report_id);
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 650));
    }
  } catch (error) {
    polling = false;
    loadingPanel.hidden = true;
    document.body.classList.remove("is-researching");
    showQueryError(error.message || "分析失败，请检查网络及API配置。", error.code || "RESEARCH_FAILED");
  }
}

$("#searchForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = $("#companyQuery").value.trim();
  if (!query) return $("#companyQuery").focus();
  const config = runtimeConfig.checked ? runtimeConfig : await refreshConfigStatus();
  if (!config.ready) {
    const detail = config.missing.length
      ? `当前缺少 ${config.missing.join("、")}。请在项目根目录 .env 中填写密钥并重启服务；密钥不会被打包进程序。`
      : `当前无法确认后端服务状态：${config.error || "请确认服务已启动"}。`;
    showQueryError(detail, "API_CONFIGURATION_REQUIRED");
    return;
  }
  await runAnalysis(query);
});
$("#cancelLoading")?.addEventListener("click", () => {
  polling = false;
  $("#loadingPanel").hidden = true;
  document.body.classList.remove("is-researching");
  $("#searchSection")?.scrollIntoView({ behavior: "smooth", block: "start" });
});

function riskClass(level = "") {
  if (/重大|极高/.test(level)) return "critical";
  if (/高/.test(level)) return "high";
  if (/中|重点/.test(level)) return "medium";
  return "low";
}
async function loadHistory() {
  const list = $("#historyList");
  list.innerHTML = '<p class="history-empty">正在读取历史记录…</p>';
  try {
    const data = await fetchJSON("/api/history?limit=30");
    const items = data.items || [];
    list.innerHTML = items.length ? items.map((item) => `<article class="history-item" data-report-id="${escapeHTML(item.report_id)}">
      <button type="button" class="history-open"><span><strong>${escapeHTML(item.company_name || item.query)}</strong><small>${escapeHTML(item.stock_code || item.credit_code || item.query || "")}</small></span><em class="${riskClass(item.risk_level)}">${escapeHTML(item.risk_level || "已完成")}</em></button>
      <time>${escapeHTML(item.created_at || "")}</time>
    </article>`).join("") : '<p class="history-empty">暂无历史查询。完成一次企业查询后会自动保留完整报告。</p>';
    $$(".history-open", list).forEach((button) => button.addEventListener("click", () => goToReport(button.closest(".history-item").dataset.reportId)));
  } catch (error) {
    list.innerHTML = `<p class="history-empty">历史记录读取失败：${escapeHTML(error.message)}</p>`;
  }
}
function openHistory() {
  const drawer = $("#historyDrawer");
  drawer.classList.add("is-open");
  drawer.setAttribute("aria-hidden", "false");
  document.body.classList.add("history-open");
  loadHistory();
}
function closeHistory() {
  const drawer = $("#historyDrawer");
  drawer.classList.remove("is-open");
  drawer.setAttribute("aria-hidden", "true");
  document.body.classList.remove("history-open");
}
$$('[data-history-open]').forEach((button) => button.addEventListener("click", openHistory));
$$('[data-history-close]').forEach((button) => button.addEventListener("click", closeHistory));
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeHistory(); });

try {
  const params = new URLSearchParams(window.location.search);
  const directQueryMode = params.get("query") === "1" || sessionStorage.getItem("chain-return-query") === "1";
  if (directQueryMode) {
    sessionStorage.removeItem("chain-return-query");
    openQueryWorkspace({ restoreQuery: false });
  } else {
    $("#searchForm")?.reset();
    if ($("#companyQuery")) $("#companyQuery").value = "";
  }
} catch (_) {}

// The application has a dedicated report archive.  Do not create a second, browser-
// managed query history dropdown beneath the search field.
const queryField = $("#companyQuery");
if (queryField) {
  queryField.value = "";
  queryField.readOnly = true;
  queryField.setAttribute("autocomplete", "new-password");
  queryField.setAttribute("name", `company_lookup_no_history_${crypto?.randomUUID?.() || Date.now().toString(36)}`);
  const unlockQueryField = () => {
    queryField.readOnly = false;
    queryField.value = "";
    queryField.setAttribute("autocomplete", "new-password");
  };
  queryField.addEventListener("pointerdown", unlockQueryField, { once: true });
  queryField.addEventListener("keydown", unlockQueryField, { once: true });
  queryField.addEventListener("focus", () => setTimeout(unlockQueryField, 0), { once: true });
}
$("#searchForm")?.setAttribute("autocomplete", "off");
refreshConfigStatus();
