"use strict";

const CHAIN_BUILD_ID = "20260802.4"; // compatibility: 20260729.3

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const state = {
  report: null,
  graph: null,
  graphRaw: null,
  renderedFilter: "",
  graphScale: 1,
  graphX: 0,
  graphY: 0,
  activeFilter: "all",
  highlighted: new Set(),
  drag: null,
  polling: false,
  decisionRows: [],
  decisionFilter: "all",
  pendingGauge: null,
};

const escapeHTML = (value) => String(value ?? "").replace(/[&<>'"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" }[ch]));
const formatNumber = (value, digits = 0) => Number(value || 0).toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const shortRiskLabel = (value) => {
  const text = String(value || "");
  if (text.includes("高")) return "高";
  if (text.includes("中")) return "中";
  if (text.includes("低")) return "低";
  return "待核验";
};
const formatMoney = (value) => `¥${Number(value || 0).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`;
const riskColor = (risk) => ({ critical: "#e43f50", high: "#ff6571", medium: "#f2b84b", low: "#35c995", unknown: "#87909f", core: "#3f8cff" }[risk] || "#87909f");
const riskText = (risk) => ({ critical: "重大风险", high: "高风险", medium: "重点关注", low: "一般关联", unknown: "未定级", core: "核心企业" }[risk] || risk || "未定级");
function nodeColor(node) {
  if (node.risk === "core") return "#3f8cff";
  if (["critical", "high"].includes(node.risk)) return riskColor(node.risk);
  const text = `${node.category || ""} ${node.relation_type || ""}`;
  if (/司法|诉讼|执行|处罚|冻结|风险事件/.test(text)) return "#e25463";
  if (/担保|质押/.test(text)) return "#d9a441";
  if (/股权|控股|股东|实际控制|持股/.test(text)) return "#9275e8";
  if (/投资|子公司|参股/.test(text)) return "#31b9aa";
  if (/交易|客户|供应商|采购|销售/.test(text)) return "#e18447";
  if (node.risk === "medium") return "#edae43";
  if (node.risk === "low") return "#37bd89";
  return "#6e9ea0";
}
function relationClass(relation = "") {
  if (/担保|质押/.test(relation)) return "guarantee";
  if (/冻结|诉讼|执行|处罚|风险/.test(relation)) return "risk";
  if (/股权|控股|股东|控制|持股/.test(relation)) return "equity";
  if (/投资|子公司|参股/.test(relation)) return "investment";
  if (/交易|客户|供应商/.test(relation)) return "trade";
  return "other";
}
const isMissing = (value) => value === null || value === undefined || value === "" || String(value).trim().toLowerCase() === "null" || String(value).trim().toLowerCase() === "undefined";
const unknownText = (value, suffix = "") => isMissing(value) ? "未获取" : `${value}${suffix}`;

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", headers: { "Content-Type": "application/json", "X-Chain-Risk-Build": CHAIN_BUILD_ID, ...(options.headers || {}) }, ...options });
  let data = {};
  try { data = await response.json(); } catch (_) { data = {}; }
  if (!response.ok) {
    const error = new Error(data.detail || `请求失败（${response.status}）`);
    error.code = data.code || `HTTP_${response.status}`;
    throw error;
  }
  return data;
}


function animateRiskGauge(score, color, available) {
  const gauge = $("#riskGauge");
  if (!gauge) return;
  const target = available ? Math.max(0, Math.min(360, Number(score || 0) * 3.6)) : 18;
  const activeColor = available ? color : "#9a7f82";
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    gauge.style.background = `conic-gradient(${activeColor} 0 ${target}deg, rgba(104,17,25,.12) ${target}deg 360deg)`;
    return;
  }
  const started = performance.now();
  const duration = 1350;
  const tick = (now) => {
    const progress = Math.min(1, (now - started) / duration);
    const eased = 1 - Math.pow(1 - progress, 4);
    const degree = target * eased;
    gauge.style.background = `conic-gradient(${activeColor} 0 ${degree}deg, rgba(104,17,25,.12) ${degree}deg 360deg)`;
    if (progress < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function renderCreditJudgment(summary) {
  const judgment = summary.credit_judgment || {};
  const grade = shortRiskLabel(judgment.grade || summary.risk_level);
  const orbit = $('#judgmentOrbit');
  if (orbit) {
    orbit.classList.remove('grade-high', 'grade-medium', 'grade-low', 'grade-pending');
    orbit.classList.add(grade === '高' ? 'grade-high' : grade === '中' ? 'grade-medium' : grade === '低' ? 'grade-low' : 'grade-pending');
  }
  $('#judgmentGrade').textContent = grade;
  $('#judgmentStance').textContent = judgment.stance || '待核验';
  $('#judgmentHeadline').textContent = judgment.headline || '关键证据尚未形成闭环';
  $('#judgmentSummary').textContent = judgment.summary || '请补齐关键证据后重新形成三档判断。';
  const briefBox = $('#riskBrief');
  if (briefBox) {
    briefBox.innerHTML = '';
    briefBox.hidden = true;
  }
  const suggestions = Array.isArray(judgment.personalized_suggestions) && judgment.personalized_suggestions.length
    ? judgment.personalized_suggestions
    : (Array.isArray(judgment.actions) ? judgment.actions : []);
  const suggestionBox = $('#judgmentSuggestions');
  if (suggestionBox) {
    suggestionBox.innerHTML = suggestions.slice(0, 3).map((item, index) => `
      <li style="--suggestion-delay:${index * 90}ms"><b>${String(index + 1).padStart(2, '0')}</b><span>${escapeHTML(item)}</span></li>
    `).join('') || '<li><b>01</b><span>建议补齐最新财务、征信和司法材料后再形成审查条件。</span></li>';
  }
  const logic = Array.isArray(judgment.logic) ? judgment.logic : ['主体核验','证据归因','偿债压力测试','三档判定'];
  $('#judgmentLogic').innerHTML = logic.map((item, index) => `<li style="--logic-delay:${index * 120}ms"><i></i><span>${escapeHTML(item)}</span></li>`).join('');
  const confidence = Math.max(0, Math.min(100, Number(judgment.confidence_percent ?? summary.confidence ?? 0)));
  const confidenceText = $('#judgmentConfidence');
  const orbitPercent = $('#judgmentOrbitPercent');
  const bar = $('#judgmentConfidenceBar');
  if (bar) bar.style.setProperty('--confidence-width', `${confidence}%`);
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduceMotion) {
    if (confidenceText) confidenceText.textContent = `${judgment.confidence_label || '—'} · ${confidence.toFixed(1)}%`;
    if (orbitPercent) orbitPercent.textContent = `${confidence.toFixed(0)}%`;
  } else {
    const started = performance.now();
    const tick = (now) => {
      const progress = Math.min(1, (now - started) / 760);
      const value = confidence * (1 - Math.pow(1 - progress, 3));
      if (confidenceText) confidenceText.textContent = `${judgment.confidence_label || '—'} · ${value.toFixed(1)}%`;
      if (orbitPercent) orbitPercent.textContent = `${value.toFixed(0)}%`;
      if (progress < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }
  document.body.dataset.riskGrade = grade;
}

function cleanCompanyDisplayValue(label, value) {
  let text = String(value ?? '').replace(/\s+/g, ' ').trim();
  if (!text || ['未获取','待核验','未发生变化','营业执照注册号','法定代表人','实际控制人','控股股东'].includes(text)) return '';
  const nextLabels = ['统一社会信用代码','营业执照注册号','法定代表人','法人代表','注册地址','注册资本','经营范围','成立日期','企业状态','经营状态','所属行业','控股股东','实际控制人','主要产品'];
  const escaped = nextLabels.map((item) => item.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  if (label === '注册地址') text = text.split(new RegExp(`(?:${escaped})\\s*[:：]?`))[0].trim();
  if (label === '法定代表人') text = text.split(/[；。，,|\s]/, 1)[0].trim();
  if (label === '控股股东' || label === '实际控制人') {
    if (/^[)）]/.test(text) || /未发生变化|营业执照|注册号/.test(text)) return '';
    text = text.split(/(?:持有|持股|提供担保|未发生变化|；|。|，)/, 1)[0].trim();
  }
  return text.length > 120 ? `${text.slice(0, 117)}…` : text;
}

function renderReport(report) {
  const c = report.company || {};
  const s = report.summary || {};
  $("#reportIdText").textContent = `报告编号：${String(report.report_id || "").slice(0, 12).toUpperCase()}`;
  $("#companyName").textContent = c.name || c.credit_code || report.query || "企业主体尚未核验";
  const info = [
    ["股票代码", c.stock_code], ["统一社会信用代码", c.credit_code], ["法定代表人", c.legal_person], ["注册地址", c.registered_address],
    ["注册资本", c.registered_capital], ["企业状态", c.company_status], ["控股股东", c.controlling_shareholder], ["实际控制人", c.actual_controller],
    ["所属行业", c.industry], ["成立日期", c.established_date], ["主要产品", c.main_products], ["分析日期", c.analysis_date],
  ];
  $("#companyInfoGrid").innerHTML = info.map(([label, value], index) => {
    const cleanValue = cleanCompanyDisplayValue(label, value);
    return `<article class="info-card report-reveal-item" style="--item-delay:${index * 35}ms"><small>${escapeHTML(label)}</small><strong>${escapeHTML(cleanValue || "待核验")}</strong></article>`;
  }).join("");

  const scoreAvailable = s.risk_score !== null && s.risk_score !== undefined;
  const score = scoreAvailable ? Number(s.risk_score) : 0;
  $("#riskScore").textContent = scoreAvailable ? formatNumber(score, 1) : "—";
  $("#riskLevel").textContent = shortRiskLabel(s.risk_level);
  $("#riskCaption").textContent = scoreAvailable ? `公开证据初筛分 · 置信度${unknownText(s.confidence, "%")}` : "身份或证据覆盖不足，暂不评分";
  renderCreditJudgment(s);
  const gaugeColor = score >= 60 ? "#b11627" : score >= 40 ? "#d68a2f" : "#238b67";
  state.pendingGauge = { score, color: gaugeColor, available: scoreAvailable };
  $("#riskGauge").style.background = `conic-gradient(${scoreAvailable ? gaugeColor : "#9a7f82"} 0 0deg, rgba(104,17,25,.12) 0deg 360deg)`;

  const metrics = [
    ["关联企业数量", `${Number(s.related_company_count || 0)}家`],
    ["异常交易信号", `${Number(s.abnormal_transaction_count || 0)}项`],
    ["涉诉及风险金额", s.involved_amount_yi === null || s.involved_amount_yi === undefined ? "未获取" : `${s.involved_amount_yi}亿元`],
    ["数据完整度", `${Number(s.data_completeness || 0).toFixed(1)}%`],
  ];
  $("#metricsGrid").innerHTML = metrics.map(([label, value], index) => `<article class="metric-card report-reveal-item" style="--item-delay:${index * 70 + 90}ms"><small>${escapeHTML(label)}</small><strong data-display-value="${escapeHTML(value)}">${escapeHTML(value)}</strong><i aria-hidden="true"></i></article>`).join("");
  $("#analysisTime").textContent = `${s.analysis_completed_at || "—"} · ${unknownText(s.analysis_seconds, "秒")}`;
  $("#dataSources").textContent = `${Math.max(0, Number(s.source_count || 0))}个相关公开来源`;
  const tokenCount = Number(s.total_tokens || modelTokenUsage(report) || 0);
  const modelCalls = Number(s.model_calls || 0);
  $("#searchUsage").textContent = s.cache_hit
    ? "缓存命中 · 0新增Token"
    : `${Number(s.search_queries || 0)}次检索 · ${Number(s.search_credits || 0)} credits · ${modelCalls}次模型调用 · ${tokenCount.toLocaleString("zh-CN")} tokens`;
  const model = report.model || {};
  $("#modelStatus").textContent = model.review_used ? `${unknownText(model.fast_model)} + ${unknownText(model.review_model)}` : unknownText(model.fast_model || model.model_label || "DeepSeek V4 Flash");
  $("#dataModeBadge").textContent = s.cache_hit ? "可信缓存" : (s.identity_verified ? "Tavily实时检索 · 身份已核验" : "身份待核验");

  renderDimensionChart(report.risk_dimensions || []);
  renderTrendChart(report.trend || { labels: [], values: [] });
  renderGraph(report.graph || { nodes: [], edges: [] });
  renderDecisionCards(report.decision_cards || [], report.findings || [], report.recommendations || []);
  renderList($("#findingsList"), report.findings || []);
  renderList($("#recommendationsList"), report.recommendations || []);
  renderShap(report.shap || []);
  populateRoi(report.roi || {});
  renderTrace(report.decision_trace || []);
  renderSources(report.sources || [], report.missing_fields || [], report.conflicts || [], s);
}

function modelTokenUsage(report) {
  return Number(report?.model?.token_usage?.total_tokens || 0);
}


function activateReportMotion() {
  bindPremiumMotionItems();
  $$(".report-reveal-item").forEach((item) => item.classList.add("is-shown"));
  $$(".metric-card strong[data-display-value]").forEach((node) => animateMetricValue(node));
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const sections = $$(".result-enter");
  if (!("IntersectionObserver" in window) || reduceMotion) {
    sections.forEach((section) => section.classList.add("is-visible"));
  } else {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      });
    }, { threshold: .12, rootMargin: "0px 0px -8%" });
    sections.forEach((section) => observer.observe(section));
  }

  const playGauge = () => {
    if (!state.pendingGauge) return;
    const target = state.pendingGauge;
    state.pendingGauge = null;
    animateRiskGauge(target.score, target.color, target.available);
  };
  const chartPanels = $$(".chart-panel");
  const summaryPanel = $(".summary-panel");
  const playPanel = (panel) => {
    if (panel === summaryPanel) playGauge();
    $$(".bar-row", panel).forEach((row) => row.classList.add("is-drawn"));
    $(".trend-animated", panel)?.classList.add("is-drawn");
  };
  if (!("IntersectionObserver" in window) || reduceMotion) {
    if (summaryPanel) playPanel(summaryPanel);
    chartPanels.forEach(playPanel);
  } else {
    const chartObserver = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        playPanel(entry.target);
        chartObserver.unobserve(entry.target);
      });
    }, { threshold: .30, rootMargin: "0px 0px -7%" });
    if (summaryPanel) chartObserver.observe(summaryPanel);
    chartPanels.forEach((panel) => chartObserver.observe(panel));
  }

  requestAnimationFrame(() => {
    $("#overviewSection")?.classList.add("is-visible");
    $$(".result-fragment").forEach((item) => item.classList.add("is-visible"));
  });
}


function animateMetricValue(node) {
  const raw = node.dataset.displayValue || node.textContent || "";
  const match = raw.match(/^([0-9]+(?:\.[0-9]+)?)(.*)$/);
  if (!match || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const target = Number(match[1]);
  const suffix = match[2];
  const decimals = match[1].includes(".") ? match[1].split(".")[1].length : 0;
  const started = performance.now();
  const duration = 620;
  const tick = (now) => {
    const progress = Math.min(1, (now - started) / duration);
    const eased = 1 - Math.pow(1 - progress, 3);
    node.textContent = `${(target * eased).toFixed(decimals)}${suffix}`;
    if (progress < 1) requestAnimationFrame(tick);
    else node.textContent = raw;
  };
  requestAnimationFrame(tick);
}


function graphRelationBucket(relation = "", node = null) {
  const text = `${relation || ""} ${node?.category || ""} ${node?.relation_type || ""}`;
  if (/司法|诉讼|执行|处罚|冻结|风险事件|风险信号/.test(text)) return "risk";
  if (/担保|质押/.test(text)) return "guarantee";
  if (/股权|控股|股东|控制|持股/.test(text)) return "equity";
  if (/投资|子公司|参股/.test(text)) return "investment";
  if (/交易|客户|供应商|采购|销售|往来/.test(text)) return "trade";
  return "other";
}
function graphBucketTitle(bucket) {
  return ({ equity: "股权控制", investment: "投资子公司", trade: "交易关系", guarantee: "担保质押", risk: "风险事项", other: "其他关联" }[bucket] || "其他关联");
}
function compactNodeName(name = "", line = 8) {
  const chars = Array.from(String(name || "未命名主体"));
  const first = chars.slice(0, line).join("");
  const secondRaw = chars.slice(line, line * 2).join("");
  const second = chars.length > line * 2 ? `${secondRaw}…` : secondRaw;
  return second
    ? `<tspan x="0" dy="0">${escapeHTML(first)}</tspan><tspan x="0" dy="16">${escapeHTML(second)}</tspan>`
    : `<tspan x="0" dy="0">${escapeHTML(first)}</tspan>`;
}

function filterGraphData(rawGraph, filter = "all") {
  const raw = rawGraph || { nodes: [], edges: [] };
  const nodes = (raw.nodes || []).map((node) => ({ ...node }));
  const edges = (raw.edges || []).map((edge, index) => ({ ...edge, id: edge.id || `e${index}` }));
  if (!nodes.length) return { nodes, edges, showEdgeLabels: false, overlays: [] };
  const coreId = (nodes.find((node) => node.risk === "core") || nodes[0]).id;
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  let keptEdges = edges;
  if (filter !== "all") {
    keptEdges = edges.filter((edge) => {
      const bucket = graphRelationBucket(edge.relation);
      if (filter === "risk") {
        const source = nodeById.get(edge.source), target = nodeById.get(edge.target);
        const riskNode = [source, target].some((node) => node && ["critical", "high", "medium"].includes(node.risk));
        return bucket === "risk" || riskNode;
      }
      return bucket === filter;
    });
  }
  const keepIds = new Set([coreId]);
  keptEdges.forEach((edge) => { keepIds.add(edge.source); keepIds.add(edge.target); });
  const keptNodes = nodes.filter((node) => keepIds.has(node.id));
  return { nodes: keptNodes, edges: keptEdges, showEdgeLabels: filter !== "all", overlays: [] };
}
function layoutFocusedGraph(graph) {
  const cx = 600, cy = 358;
  const nodes = (graph.nodes || []).map((node) => ({ ...node }));
  const edges = (graph.edges || []).map((edge) => ({ ...edge }));
  if (!nodes.length) return { nodes, edges, showEdgeLabels: graph.showEdgeLabels, overlays: [] };
  const core = nodes.find((n) => n.risk === "core") || nodes[0];
  core.x = cx; core.y = cy;
  const others = nodes.filter((n) => n.id !== core.id).sort((a, b) => {
    const pa = { critical: 0, high: 1, medium: 2, low: 3, unknown: 4 }[a.risk] ?? 9;
    const pb = { critical: 0, high: 1, medium: 2, low: 3, unknown: 4 }[b.risk] ?? 9;
    return pa - pb || String(a.name).localeCompare(String(b.name), "zh-CN");
  });
  const rings = [
    { radius: 160, capacity: 8, yScale: 0.84 },
    { radius: 240, capacity: 12, yScale: 0.80 },
    { radius: 320, capacity: 16, yScale: 0.76 },
    { radius: 390, capacity: 22, yScale: 0.72 },
  ];
  let cursor = 0;
  rings.forEach((ring, ringIndex) => {
    const slice = others.slice(cursor, cursor + ring.capacity);
    cursor += slice.length;
    const start = -Math.PI / 2 + ringIndex * 0.18;
    slice.forEach((node, idx) => {
      const angle = start + (idx / Math.max(slice.length, 1)) * Math.PI * 2;
      const offset = idx % 2 ? 8 : -8;
      node.x = cx + Math.cos(angle) * (ring.radius + offset);
      node.y = cy + Math.sin(angle) * (ring.radius + offset) * ring.yScale;
    });
  });
  others.slice(cursor).forEach((node, idx, arr) => {
    const angle = -Math.PI / 2 + (idx / Math.max(arr.length, 1)) * Math.PI * 2;
    node.x = cx + Math.cos(angle) * 430;
    node.y = cy + Math.sin(angle) * 300;
  });
  return { nodes, edges, showEdgeLabels: graph.showEdgeLabels, overlays: [] };
}
function layoutLayeredOverview(graph) {
  const cx = 600, cy = 356;
  const nodes = (graph.nodes || []).map((node) => ({ ...node }));
  const edges = (graph.edges || []).map((edge) => ({ ...edge }));
  if (!nodes.length) return { nodes, edges, showEdgeLabels: false, overlays: [] };
  const core = nodes.find((node) => node.risk === "core") || nodes[0];
  core.x = cx; core.y = cy;

  const bucketOrder = ["risk", "equity", "investment", "trade", "guarantee", "other"];
  const groups = new Map(bucketOrder.map((key) => [key, []]));
  nodes.filter((node) => node.id !== core.id).forEach((node) => {
    const relatedEdges = edges.filter((edge) => edge.source === node.id || edge.target === node.id);
    const score = { risk: 0, guarantee: 0, equity: 0, investment: 0, trade: 0, other: 0 };
    relatedEdges.forEach((edge) => { score[graphRelationBucket(edge.relation, node)] += 1; });
    if (["critical", "high"].includes(node.risk) && /风险|司法|诉讼|执行|处罚|冻结/.test(`${node.category} ${node.relation_type}`)) score.risk += 10;
    const bucket = Object.entries(score).sort((a, b) => b[1] - a[1])[0]?.[0] || "other";
    (groups.get(bucket) || groups.get("other")).push(node);
  });
  bucketOrder.forEach((key) => groups.get(key).sort((a, b) => String(a.name).localeCompare(String(b.name), "zh-CN")));
  const ordered = bucketOrder.flatMap((key) => groups.get(key));
  const total = Math.max(1, ordered.length);
  const overlays = [];
  const groupPoints = new Map(bucketOrder.map((key) => [key, []]));
  const startAngle = -Math.PI / 2;

  ordered.forEach((node, index) => {
    const angle = startAngle + (index + 0.5) / total * Math.PI * 2;
    const ringIndex = total <= 16 ? index % 2 : index % 3;
    const baseRadius = total <= 16 ? 250 : 245;
    const radius = baseRadius + ringIndex * (total <= 16 ? 120 : 98) + ((index % 2) ? 10 : -7);
    node.x = Math.max(58, Math.min(1142, cx + Math.cos(angle) * radius));
    node.y = Math.max(64, Math.min(656, cy + Math.sin(angle) * radius * 0.66));
    const bucket = bucketOrder.find((key) => groups.get(key).includes(node)) || "other";
    groupPoints.get(bucket).push([node.x, node.y]);
  });

  bucketOrder.forEach((key) => {
    const points = groupPoints.get(key);
    if (!points.length) return;
    const x = points.reduce((sum, point) => sum + point[0], 0) / points.length;
    const y = points.reduce((sum, point) => sum + point[1], 0) / points.length;
    const dx = x - cx, dy = y - cy;
    const length = Math.hypot(dx, dy) || 1;
    overlays.push({
      x: Math.max(92, Math.min(1108, x + dx / length * 42)),
      y: Math.max(52, Math.min(678, y + dy / length * 34)),
      label: `${graphBucketTitle(key)} · ${points.length}`,
    });
  });
  return { nodes, edges, showEdgeLabels: false, overlays };
}

function rebuildGraphView() {
  if (!state.graphRaw) return;
  const stage = $(".graph-stage");
  stage?.classList.add("is-switching");
  const prepared = filterGraphData(state.graphRaw, state.activeFilter || "all");
  state.graph = (state.activeFilter || "all") === "all" ? layoutLayeredOverview(prepared) : layoutFocusedGraph(prepared);
  state.renderedFilter = state.activeFilter || "all";
  requestAnimationFrame(() => {
    drawGraph();
    applyGraphTransform();
    $("#nodeDetail").hidden = true;
    requestAnimationFrame(() => stage?.classList.remove("is-switching"));
  });
}


function renderDecisionCards(cards, findings, recommendations) {
  const fallback = (findings || []).slice(0, 6).map((finding, index) => ({
    severity: index === 0 ? "medium" : "low",
    label: "公开信息初筛",
    title: String(finding).split(/[：｜]/)[0] || `审查事项${index + 1}`,
    summary: finding,
    impact: "该事项需要结合原始证据、企业材料和银行内部数据判断实际授信影响。",
    action: recommendations[index] || "回看原始证据并完成客户经理人工核验。",
    source_ids: [],
  }));
  const severityOrder = { critical: 4, high: 3, medium: 2, low: 1 };
  state.decisionRows = (cards.length ? cards : fallback)
    .map((card) => ({ ...card, severity: card.severity === "info" ? "medium" : (severityOrder[card.severity] ? card.severity : "low") }))
    .sort((a, b) => severityOrder[b.severity] - severityOrder[a.severity]);
  const firstAvailable = ["critical", "high", "medium", "low"].find((level) => state.decisionRows.some((row) => row.severity === level)) || "low";
  state.decisionFilter = firstAvailable;
  renderRiskPyramid();
  renderDecisionMatrix();
}

function renderRiskPyramid() {
  const counts = { critical: 0, high: 0, medium: 0, low: 0 };
  state.decisionRows.forEach((row) => { counts[row.severity] = (counts[row.severity] || 0) + 1; });
  $$('[data-decision-filter]').forEach((button) => {
    const key = button.dataset.decisionFilter;
    const countNode = button.querySelector('b');
    if (countNode) countNode.textContent = String(counts[key] ?? 0);
    button.classList.toggle('active', key === state.decisionFilter);
    button.onclick = () => {
      state.decisionFilter = key;
      renderRiskPyramid();
      renderDecisionMatrix();
    };
  });
}

function renderDecisionMatrix() {
  const target = $('#decisionCards');
  const filtered = state.decisionRows.filter((row) => row.severity === state.decisionFilter);
  if (!filtered.length) {
    target.innerHTML = '<div class="decision-empty">该风险等级下暂无已核验事项。</div>';
    return;
  }
  const levelTextMap = { critical: '重大风险', high: '高风险', medium: '中风险', low: '低风险' };
  target.innerHTML = filtered.map((card, index) => {
    const severity = card.severity;
    const sourceIds = (card.source_ids || []).slice(0, 8);
    return `<article class="decision-pair severity-${severity}" data-severity="${severity}">
      <div class="decision-severity"><i aria-hidden="true"></i><span>${escapeHTML(levelTextMap[severity] || '一般风险')}</span><b>${String(index + 1).padStart(2, '0')}</b></div>
      <div class="decision-risk-column">
        <div class="decision-column-label"><span aria-hidden="true">!</span><strong>风险</strong></div>
        <h3>${escapeHTML(card.title || '待核验事项')}</h3>
        <p>${escapeHTML(card.summary || '公开来源记录了该事项。')}</p>
        <div class="decision-impact-line"><strong>授信影响</strong><p>${escapeHTML(card.impact || '需结合原始材料判断实际影响。')}</p></div>
      </div>
      <div class="decision-link" aria-hidden="true"><span>→</span></div>
      <div class="decision-advice-column">
        <div class="decision-column-label"><span aria-hidden="true">✓</span><strong>建议</strong></div>
        <p>${escapeHTML(card.action || '由客户经理回看原始证据并完成人工核验。')}</p>
        ${sourceIds.length ? `<button type="button" class="evidence-jump" data-source-ids="${escapeHTML(sourceIds.join(','))}">查看证据 ↗</button>` : ''}
      </div>
    </article>`;
  }).join('');
  $$('.evidence-jump', target).forEach((button) => button.addEventListener('click', () => {
    const ids = String(button.dataset.sourceIds || '').split(',').filter(Boolean);
    const row = ids.map((id) => $(`.source-row[data-source-id="${CSS.escape(id)}"]`)).find(Boolean);
    if (row) {
      row.classList.add('is-open');
      row.querySelector('.source-summary')?.setAttribute('aria-expanded', 'true');
      row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } else {
      $('.evidence-ledger')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }));
}

function renderList(element, rows) {
  const isAdvice = element.id === "recommendationsList";
  const emptyText = isAdvice ? "暂未生成处置建议" : "暂未识别到可展示风险";
  element.innerHTML = rows.length
    ? rows.map((row, index) => `<li><span class="insight-index">${String(index + 1).padStart(2, "0")}</span><p>${highlightInsightText(row, isAdvice)}</p><i aria-hidden="true">${isAdvice ? "↗" : "•"}</i></li>`).join("")
    : `<li class="insight-empty"><span class="insight-index">—</span><p>${emptyText}</p></li>`;
}

function highlightInsightText(value) {
  return escapeHTML(value).replace(/(\d+(?:\.\d+)?(?:%|亿元|万元|元|家|项|笔|分|天|秒)?)/g, '<strong class="insight-number">$1</strong>');
}


function renderDimensionChart(items) {
  const target = $("#dimensionChart");
  target.innerHTML = items.length
    ? items.map((item, index) => `<div class="bar-row chart-enter" style="--chart-delay:${index * 130}ms"><span>${escapeHTML(item.name)}</span><div class="bar-track"><div class="bar-fill" data-target-width="${Math.min(100, Number(item.score || 0))}"></div></div><b>${formatNumber(item.score, 1)}</b></div>`).join("")
    : '<div class="empty-chart">暂无可量化风险维度</div>';
  $$(".bar-fill", target).forEach((bar) => { bar.style.setProperty("--bar-width", `${bar.dataset.targetWidth || 0}%`); });
}

function renderTrendChart(trend) {
  const labels = trend.labels || [];
  const values = trend.values || [];
  if (!labels.length || !values.length) {
    $("#trendChart").innerHTML = '<div class="empty-chart">未获取到带明确日期的风险事件，暂不生成趋势线</div>';
    return;
  }
  const width = 620, height = 240, left = 42, right = 22, top = 20, bottom = 42;
  const plotW = width - left - right, plotH = height - top - bottom;
  const maxY = Math.max(100, Math.ceil(Math.max(...values, 10) / 20) * 20);
  const points = values.map((v, i) => ({ x: left + (labels.length <= 1 ? plotW / 2 : i * plotW / (labels.length - 1)), y: top + plotH - (v / maxY) * plotH, v, label: labels[i] }));
  const polyline = points.map((p) => `${p.x},${p.y}`).join(" ");
  const area = `${left},${top + plotH} ${polyline} ${left + plotW},${top + plotH}`;
  const horizontal = [0, 25, 50, 75, 100].map((tick) => {
    const y = top + plotH - (tick / maxY) * plotH;
    return `<line class="trend-grid" x1="${left}" y1="${y}" x2="${left + plotW}" y2="${y}"/><text class="trend-label" x="4" y="${y + 4}">${tick}</text>`;
  }).join("");
  const dots = points.map((p, index) => `<g class="trend-point" style="--point-delay:${index * 180}ms"><circle class="trend-dot" cx="${p.x}" cy="${p.y}" r="5"/><text class="trend-value" x="${p.x}" y="${p.y - 12}" text-anchor="middle">${p.v}</text><text class="trend-label" x="${p.x}" y="${top + plotH + 25}" text-anchor="middle">${escapeHTML(p.label)}</text></g>`).join("");
  const target = $("#trendChart");
  target.innerHTML = `<svg class="trend-animated" viewBox="0 0 ${width} ${height}" aria-label="风险趋势折线图"><defs><linearGradient id="trendGradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#c52232" stop-opacity=".28"/><stop offset="1" stop-color="#c52232" stop-opacity="0"/></linearGradient></defs>${horizontal}<polygon class="trend-area" points="${area}"/><polyline class="trend-line" points="${polyline}"/>${dots}</svg>`;
}


function graphLayout(graph) {
  return (state.activeFilter || 'all') === 'all' ? layoutLayeredOverview(graph) : layoutFocusedGraph(graph);
}

function renderGraph(graph) {
  state.graphRaw = { nodes: (graph.nodes || []).map((node) => ({ ...node })), edges: (graph.edges || []).map((edge, index) => ({ ...edge, id: edge.id || `e${index}` })) };
  state.graphScale = 1;
  state.graphX = 0;
  state.graphY = 0;
  state.activeFilter = 'all';
  state.highlighted.clear();
  $$('.filter-group button').forEach((b) => b.classList.toggle('active', b.dataset.filter === 'all'));
  $('#graphSearch').value = '';
  rebuildGraphView();
}

function drawGraph() {
  const graph = state.graph;
  if (!graph || !graph.nodes.length) {
    $('#edgesLayer').innerHTML = '';
    $('#nodesLayer').innerHTML = '<text x="600" y="350" text-anchor="middle" fill="#f5d9d9">未获取到可验证的企业关系</text>';
    return;
  }
  const nodeById = new Map(graph.nodes.map((n) => [n.id, n]));
  $('#riskGraph').classList.toggle('graph-dense', graph.nodes.length > 26);
  $('#edgesLayer').innerHTML = graph.edges.map((edge) => {
    const a = nodeById.get(edge.source), b = nodeById.get(edge.target);
    if (!a || !b) return '';
    const cls = relationClass(edge.relation);
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    const label = String(edge.relation || '关联');
    const labelWidth = Math.max(58, Math.min(220, Array.from(label).length * 15 + 24));
    return `<g class="graph-edge-group ${cls} ${graph.showEdgeLabels ? 'labels-on' : 'labels-off'}" data-edge-id="${edge.id}" data-relation="${escapeHTML(label)}">
      <line class="graph-edge ${cls}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke-width="${1.4 + Number(edge.weight || .5) * 2.9}"/>
      ${graph.showEdgeLabels ? `<g class="edge-label-wrap" transform="translate(${mx} ${my})"><rect x="${-labelWidth / 2}" y="-13" width="${labelWidth}" height="25" rx="9"/><text class="edge-label" y="4" text-anchor="middle">${escapeHTML(label)}</text></g>` : ''}
    </g>`;
  }).join('');
  const overlays = (graph.overlays || []).map((item) => `<g class="graph-cluster-label" transform="translate(${item.x} ${item.y})"><rect x="-64" y="-16" width="128" height="30" rx="14"></rect><text text-anchor="middle" y="5">${escapeHTML(item.label)}</text></g>`).join('');
  $('#nodesLayer').innerHTML = overlays + graph.nodes.map((node) => {
    const dense = graph.nodes.length > 24;
    const r = node.risk === 'core' ? 32 : ['critical', 'high'].includes(node.risk) ? (dense ? 18 : 22) : (dense ? 15 : 18);
    return `<g class="graph-node node-${escapeHTML(node.risk || 'unknown')}" data-node-id="${escapeHTML(node.id)}" transform="translate(${node.x} ${node.y})"><title>${escapeHTML(node.name)}</title><circle class="node-halo" r="${r + 7}" fill="${nodeColor(node)}"/><circle class="node-main" r="${r}" fill="${nodeColor(node)}"/><text y="${r + 20}">${compactNodeName(node.name, dense ? 7 : 8)}</text></g>`;
  }).join('');
  bindGraphEvents();
  applyGraphVisibility();
}

function applyGraphVisibility() {
  if (!state.graphRaw) return;
  if (state.renderedFilter !== (state.activeFilter || 'all')) {
    rebuildGraphView();
    return;
  }
  const query = $('#graphSearch').value.trim().toLowerCase();
  const visibleNodeIds = new Set();
  state.graph.nodes.forEach((node) => {
    const matchesSearch = !query || node.name.toLowerCase().includes(query);
    const matchesHighlight = state.highlighted.size === 0 || state.highlighted.has(node.id);
    const visible = matchesSearch && matchesHighlight;
    if (visible) visibleNodeIds.add(node.id);
    const el = $(`.graph-node[data-node-id="${CSS.escape(node.id)}"]`, $('#riskGraph'));
    if (el) {
      el.classList.toggle('dimmed', !visible);
      el.classList.toggle('highlighted', state.highlighted.has(node.id));
      el.classList.toggle('hidden-node', !visible);
    }
  });
  state.graph.edges.forEach((edge) => {
    const endpointMatch = visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target);
    const el = $(`[data-edge-id="${edge.id}"]`, $('#riskGraph'));
    if (el) {
      el.classList.toggle('hidden-edge', !endpointMatch);
      el.querySelectorAll('line,text,rect').forEach((part) => part.classList.toggle('dimmed', !endpointMatch));
    }
  });
}

function showGraphTooltip(event, nodeId) {
  const node = state.graph.nodes.find((n) => n.id === nodeId);
  const connected = state.graph.edges.filter((e) => e.source === nodeId || e.target === nodeId);
  const tooltip = $('#graphTooltip');
  tooltip.innerHTML = `<strong>${escapeHTML(node.name)}</strong><span>所属层次：${escapeHTML(graphBucketTitle(graphRelationBucket(node.relation_type, node)))}</span><br><span>关系类型：${escapeHTML(node.relation_type || node.category)}</span><br><span>风险等级：${riskText(node.risk)}</span><br><span>直接关系：${connected.length}条</span>`;
  tooltip.hidden = false;
  moveGraphTooltip(event);
}

function showNodeDetail(nodeId) {
  const node = state.graph.nodes.find((n) => n.id === nodeId);
  const relations = state.graph.edges.filter((e) => e.source === nodeId || e.target === nodeId);
  $('#detailName').textContent = node.name;
  $('#detailBody').innerHTML = `<div class="detail-row"><small>主体信息</small><strong>所属层次：${escapeHTML(graphBucketTitle(graphRelationBucket(node.relation_type, node)))}<br>主体类型：${escapeHTML(node.company_type || node.category)}<br>风险等级：${riskText(node.risk)}</strong></div><div class="detail-row"><small>证据说明</small><strong>${escapeHTML(node.description || '未获取补充说明')}</strong></div><div class="detail-row"><small>直接关系</small>${relations.map((e) => `<span class="relation-chip">${escapeHTML(e.relation)}${e.amount ? ` · ${escapeHTML(e.amount)}` : ''}</span>`).join('') || '未获取'}</div>`;
  $('#nodeDetail').hidden = false;
}


function bindGraphEvents() {
  $$('.graph-node', $('#riskGraph')).forEach((element) => {
    const nodeId = element.dataset.nodeId;
    element.addEventListener('pointerenter', (event) => showGraphTooltip(event, nodeId));
    element.addEventListener('pointermove', moveGraphTooltip);
    element.addEventListener('pointerleave', hideGraphTooltip);
    element.addEventListener('click', (event) => {
      if (!state.drag?.moved) showNodeDetail(nodeId);
      event.stopPropagation();
    });
    element.addEventListener('dblclick', (event) => {
      event.preventDefault();
      highlightThreeHops(nodeId);
    });
    element.addEventListener('pointerdown', (event) => startNodeDrag(event, nodeId));
  });
  const svg = $('#riskGraph');
  svg.onpointermove = dragNode;
  svg.onpointerup = endNodeDrag;
  svg.onpointercancel = endNodeDrag;
}

function svgPoint(event) {
  const svg = $('#riskGraph');
  const point = svg.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  const ctm = $('#graphViewport').getScreenCTM();
  return ctm ? point.matrixTransform(ctm.inverse()) : { x: 0, y: 0 };
}

function startNodeDrag(event, nodeId) {
  const node = state.graph?.nodes.find((item) => item.id === nodeId);
  if (!node) return;
  const point = svgPoint(event);
  state.drag = { nodeId, dx: point.x - node.x, dy: point.y - node.y, moved: false, pointerId: event.pointerId };
  event.currentTarget.setPointerCapture?.(event.pointerId);
}

function updateGraphGeometry(nodeId) {
  const node = state.graph?.nodes.find((item) => item.id === nodeId);
  const nodeElement = $(`.graph-node[data-node-id="${CSS.escape(nodeId)}"]`, $('#riskGraph'));
  if (!node || !nodeElement) return;
  nodeElement.setAttribute('transform', `translate(${node.x} ${node.y})`);
  const nodeById = new Map(state.graph.nodes.map((item) => [item.id, item]));
  state.graph.edges.forEach((edge) => {
    if (edge.source !== nodeId && edge.target !== nodeId) return;
    const source = nodeById.get(edge.source), target = nodeById.get(edge.target);
    const group = $(`[data-edge-id="${CSS.escape(edge.id)}"]`, $('#riskGraph'));
    if (!source || !target || !group) return;
    const line = group.querySelector('line');
    line?.setAttribute('x1', source.x); line?.setAttribute('y1', source.y);
    line?.setAttribute('x2', target.x); line?.setAttribute('y2', target.y);
    group.querySelector('.edge-label-wrap')?.setAttribute('transform', `translate(${(source.x + target.x) / 2} ${(source.y + target.y) / 2})`);
  });
}

function dragNode(event) {
  if (!state.drag || !state.graph) return;
  const point = svgPoint(event);
  const node = state.graph.nodes.find((item) => item.id === state.drag.nodeId);
  if (!node) return;
  node.x = point.x - state.drag.dx;
  node.y = point.y - state.drag.dy;
  state.drag.moved = true;
  updateGraphGeometry(node.id);
  hideGraphTooltip();
}

function endNodeDrag() {
  if (!state.drag) return;
  const dragState = state.drag;
  setTimeout(() => {
    if (state.drag === dragState) state.drag = null;
  }, 0);
}

function moveGraphTooltip(event) {
  const stage = $('.graph-stage');
  const tooltip = $('#graphTooltip');
  if (!stage || !tooltip || tooltip.hidden) return;
  const rect = stage.getBoundingClientRect();
  tooltip.style.left = `${Math.min(rect.width - 310, Math.max(10, event.clientX - rect.left + 14))}px`;
  tooltip.style.top = `${Math.min(rect.height - 145, Math.max(10, event.clientY - rect.top + 14))}px`;
}

function hideGraphTooltip() {
  const tooltip = $('#graphTooltip');
  if (tooltip) tooltip.hidden = true;
}

function highlightThreeHops(startId) {
  if (!state.graph) return;
  const adjacency = new Map(state.graph.nodes.map((node) => [node.id, []]));
  state.graph.edges.forEach((edge) => {
    adjacency.get(edge.source)?.push(edge.target);
    adjacency.get(edge.target)?.push(edge.source);
  });
  const visited = new Set([startId]);
  let frontier = [startId];
  for (let depth = 0; depth < 3; depth += 1) {
    const next = [];
    frontier.forEach((id) => (adjacency.get(id) || []).forEach((neighbor) => {
      if (!visited.has(neighbor)) { visited.add(neighbor); next.push(neighbor); }
    }));
    frontier = next;
  }
  state.highlighted = visited;
  applyGraphVisibility();
}

function applyGraphTransform() {
  $('#graphViewport')?.setAttribute('transform', `translate(${state.graphX} ${state.graphY}) scale(${state.graphScale})`);
}

$('#closeNodeDetail')?.addEventListener('click', () => { $('#nodeDetail').hidden = true; });
$('#zoomIn')?.addEventListener('click', () => { state.graphScale = Math.min(2.2, state.graphScale + .16); applyGraphTransform(); });
$('#zoomOut')?.addEventListener('click', () => { state.graphScale = Math.max(.55, state.graphScale - .16); applyGraphTransform(); });
$('#resetGraph')?.addEventListener('click', () => {
  state.graphScale = 1; state.graphX = 0; state.graphY = 0;
  state.highlighted.clear(); state.activeFilter = 'all'; state.renderedFilter = '';
  $('#graphSearch').value = '';
  $$('.filter-group button').forEach((button) => button.classList.toggle('active', button.dataset.filter === 'all'));
  rebuildGraphView();
});

$$('.filter-group button').forEach((button) => button.addEventListener('click', () => {
  state.activeFilter = button.dataset.filter;
  state.highlighted.clear();
  $$('.filter-group button').forEach((b) => b.classList.toggle('active', b === button));
  applyGraphVisibility();
}));
$('#graphSearch').addEventListener('input', applyGraphVisibility);

function renderShap(rows) {
  if (!rows.length) {
    $("#shapBody").innerHTML = '<tr><td colspan="4">证据不足，暂不生成贡献值</td></tr>';
    return;
  }
  const max = Math.max(...rows.map((r) => Math.abs(Number(r.contribution || 0))), 1);
  $("#shapBody").innerHTML = rows.map((row) => {
    const positive = Number(row.contribution || 0) > 0;
    return `<tr><td>${escapeHTML(row.feature)}</td><td class="contrib-cell ${positive ? "contrib-positive" : "contrib-negative"}"><span class="contrib-value">${positive ? "+" : ""}${formatNumber(row.contribution, 2)}</span><span class="contrib-bar"><i style="width:${Math.abs(Number(row.contribution || 0)) / max * 100}%"></i></span></td><td class="${positive ? "direction-up" : "direction-down"}">${escapeHTML(row.direction)}</td><td>${escapeHTML(row.evidence)}</td></tr>`;
  }).join("");
}

function populateRoi(roi) {
  $("#traditionalDays").value = roi.traditional_days ?? "";
  $("#systemSeconds").value = roi.system_seconds ?? "";
  $("#loanAmount").value = roi.loan_amount ?? "";
  $("#annualRate").value = roi.annual_rate_percent ?? "";
  updateRoi();
}
function updateRoi() {
  const traditionalText = $("#traditionalDays").value.trim();
  const secondsText = $("#systemSeconds").value.trim();
  const loanText = $("#loanAmount").value.trim();
  const rateText = $("#annualRate").value.trim();
  if (!traditionalText || !secondsText || !loanText || !rateText) {
    $("#savedDays").textContent = "待填写";
    $("#savedCost").textContent = "待填写";
    return { traditional_days: traditionalText, system_seconds: secondsText, loan_amount: loanText, annual_rate: rateText, saved_days: "", saved_cost: "" };
  }
  const traditional = Number(traditionalText);
  const seconds = Number(secondsText);
  const loan = Number(loanText);
  const ratePercent = Number(rateText);
  if (![traditional, seconds, loan, ratePercent].every(Number.isFinite) || traditional < 0 || seconds < 0 || loan < 0 || ratePercent < 0) {
    $("#savedDays").textContent = "参数无效";
    $("#savedCost").textContent = "参数无效";
    return { traditional_days: traditionalText, system_seconds: secondsText, loan_amount: loanText, annual_rate: rateText, saved_days: "", saved_cost: "" };
  }
  const rate = ratePercent / 100;
  const savedDays = Math.max(0, traditional - seconds / 86400);
  const savedCost = savedDays * loan * rate / 365;
  $("#savedDays").textContent = `${formatNumber(savedDays, 4)}天`;
  $("#savedCost").textContent = formatMoney(savedCost);
  return { traditional_days: traditional, system_seconds: seconds, loan_amount: loan, annual_rate: ratePercent, saved_days: savedDays, saved_cost: savedCost };
}
$$("#traditionalDays,#systemSeconds,#loanAmount,#annualRate").forEach((input) => input.addEventListener("input", updateRoi));

function cleanTraceDetail(value) {
  return String(value || "")
    .replace(/DeepSeekResponseError/g, "模型输出已自动修复")
    .replace(/ValidationError/g, "结构校验已自动修复")
    .replace(/TavilySearchError/g, "公开检索已切换备用策略")
    .replace(/第\d+轮/g, "补充检索")
    .replace(/\s+/g, " ")
    .trim();
}

function renderTrace(items) {
  $('#decisionTrace').innerHTML = items.length
    ? items.map((item, idx) => {
      const status = String(item.status || '完成');
      const statusClass = /失败|异常/.test(status) ? 'is-warning' : /补偿|修复|降级/.test(status) ? 'is-repaired' : 'is-complete';
      return `<article class="trace-item ${statusClass}"><span class="trace-number">${String(idx + 1).padStart(2, '0')}</span><div><small>${escapeHTML(status)}</small><strong>${escapeHTML(item.stage)}</strong><p>${escapeHTML(cleanTraceDetail(item.detail))}</p></div><em>${escapeHTML(status)}</em></article>`;
    }).join('')
    : '<article class="trace-item trace-empty"><span class="trace-number">—</span><div><strong>暂无链路记录</strong></div></article>';
  requestAnimationFrame(updateTraceProgress);
}


function renderSources(sources, missingFields, conflicts, summary) {
  $("#qualitySummary").innerHTML = [
    ["公开来源", `${sources.length}个`],
    ["数据完整度", `${Number(summary.data_completeness || 0).toFixed(1)}%`],
    ["结果置信度", `${Number(summary.confidence || 0).toFixed(1)}%`],
    ["缺失字段", `${missingFields.length}项`],
    ["来源冲突", `${conflicts.length}项`],
  ].map(([label, value], index) => `<span><b>${String(index + 1).padStart(2, "0")}</b><small>${escapeHTML(label)}</small><strong>${escapeHTML(value)}</strong></span>`).join("");
  $("#sourceList").innerHTML = sources.length ? sources.map((source, index) => `
    <article class="source-row ${index === 0 ? "is-open" : ""}" data-source-id="${escapeHTML(source.id)}">
      <button class="source-summary" type="button" aria-expanded="${index === 0 ? "true" : "false"}">
        <span class="source-order">${String(index + 1).padStart(2, "0")}</span>
        <span class="source-main"><strong>${escapeHTML(source.title || "公开网页")}</strong><small>${escapeHTML(source.provider || "Tavily")} · 相关度 ${Math.round(Number(source.search_score || 0) * 100)}% · 可靠度 ${Math.round(Number(source.reliability || 0) * 100)}% · ${source.fetched ? "正文已获取" : "检索摘要"}</small></span>
        <span class="source-toggle" aria-hidden="true">＋</span>
      </button>
      <div class="source-detail"><p>${escapeHTML(source.snippet || "该来源未返回摘要")}</p><a href="${escapeHTML(source.url)}" target="_blank" rel="noopener noreferrer">核对原始来源 ↗</a></div>
    </article>`).join("") : '<div class="empty-chart">未保留可打开的公开来源</div>';
  if (missingFields.length || conflicts.length) {
    const warning = document.createElement("div");
    warning.className = "evidence-warning";
    warning.innerHTML = `${missingFields.length ? `<p><strong>缺失字段：</strong>${escapeHTML(missingFields.join("、"))}</p>` : ""}${conflicts.length ? `<p><strong>来源冲突：</strong>${escapeHTML(conflicts.join("；"))}</p>` : ""}`;
    $("#sourceList").prepend(warning);
  }
  bindSourceRows();
}

function bindSourceRows() {
  $$(".source-summary").forEach((button) => {
    button.addEventListener("click", () => {
      const row = button.closest(".source-row");
      const open = !row.classList.contains("is-open");
      row.classList.toggle("is-open", open);
      button.setAttribute("aria-expanded", String(open));
    });
  });
}

function setWorkbenchTab(tab) {
  $$("[data-workbench-tab]").forEach((button) => {
    const active = button.dataset.workbenchTab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  $$("[data-workbench-view]").forEach((view) => {
    const active = view.dataset.workbenchView === tab;
    view.hidden = !active;
    view.classList.toggle("is-active", active);
  });
}
$$("[data-workbench-tab]").forEach((button) => button.addEventListener("click", () => setWorkbenchTab(button.dataset.workbenchTab)));

const traceViewport = $("#traceViewport");
function updateTraceProgress() {
  if (!traceViewport || !$("#traceProgress")) return;
  const max = Math.max(1, traceViewport.scrollWidth - traceViewport.clientWidth);
  const ratio = Math.max(0, Math.min(1, traceViewport.scrollLeft / max));
  const visibleRatio = Math.min(1, traceViewport.clientWidth / Math.max(traceViewport.scrollWidth, 1));
  $("#traceProgress").style.width = `${Math.max(12, visibleRatio * 100)}%`;
  $("#traceProgress").style.transform = `translateX(${ratio * (100 / Math.max(visibleRatio, .12) - 100)}%)`;
}
function scrollTrace(direction) {
  if (!traceViewport) return;
  const card = traceViewport.querySelector(".trace-item");
  const distance = card ? card.getBoundingClientRect().width + 24 : 340;
  traceViewport.scrollBy({ left: distance * direction, behavior: "smooth" });
}
$("#tracePrev")?.addEventListener("click", () => scrollTrace(-1));
$("#traceNext")?.addEventListener("click", () => scrollTrace(1));
traceViewport?.addEventListener("scroll", updateTraceProgress, { passive: true });
traceViewport?.addEventListener("wheel", (event) => {
  if (Math.abs(event.deltaY) > Math.abs(event.deltaX) && traceViewport.scrollWidth > traceViewport.clientWidth) {
    event.preventDefault();
    traceViewport.scrollLeft += event.deltaY;
  }
}, { passive: false });
let traceDrag = null;
traceViewport?.addEventListener("pointerdown", (event) => {
  traceDrag = { x: event.clientX, left: traceViewport.scrollLeft };
  traceViewport.classList.add("is-dragging");
  traceViewport.setPointerCapture(event.pointerId);
});
traceViewport?.addEventListener("pointermove", (event) => {
  if (!traceDrag) return;
  traceViewport.scrollLeft = traceDrag.left - (event.clientX - traceDrag.x);
});
function endTraceDrag() { traceDrag = null; traceViewport?.classList.remove("is-dragging"); }
traceViewport?.addEventListener("pointerup", endTraceDrag);
traceViewport?.addEventListener("pointercancel", endTraceDrag);
window.addEventListener("resize", updateTraceProgress);

const revealObserver = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
  entries.forEach((entry) => { if (entry.isIntersecting) entry.target.classList.add("is-visible"); });
}, { threshold: .08, rootMargin: "0px 0px -40px" }) : null;
$$('.reveal-section').forEach((section) => revealObserver ? revealObserver.observe(section) : section.classList.add("is-visible"));

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob); const a = document.createElement("a"); a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
}
function csvCell(value) { return `"${String(value ?? "").replace(/"/g, '""')}"`; }
function downloadCSV(rows, filename) {
  const text = "\ufeff" + rows.map((row) => row.map(csvCell).join(",")).join("\r\n");
  downloadBlob(new Blob([text], { type: "text/csv;charset=utf-8" }), filename);
}
function safeFilename() {
  return String(state.report?.company?.short_name || state.report?.company?.name || "企业").replace(/[\\/:*?"<>|]/g, "_");
}

$("#exportShap").addEventListener("click", () => {
  if (!state.report) return;
  downloadCSV([["特征名称", "贡献值", "影响方向", "数据依据"], ...state.report.shap.map((r) => [r.feature, r.contribution, r.direction, r.evidence])], `${safeFilename()}_SHAP解释表.csv`);
});
$("#exportRoi").addEventListener("click", () => {
  const roi = updateRoi();
  downloadCSV([["参数", "数值"], ["传统尽调天数", roi.traditional_days], ["系统尽调秒数", roi.system_seconds], ["节省天数", roi.saved_days], ["拟贷款金额", roi.loan_amount], ["年资金成本率", roi.annual_rate], ["预计节省资金成本", roi.saved_cost]], `${safeFilename()}_ROI计算.csv`);
});
$("#exportJson").addEventListener("click", () => {
  if (!state.report) return;
  downloadBlob(new Blob([JSON.stringify(state.report, null, 2)], { type: "application/json;charset=utf-8" }), `${safeFilename()}_风险分析报告.json`);
});
$("#printReport").addEventListener("click", () => window.print());
$("#backToSearch").addEventListener("click", () => { sessionStorage.setItem("chain-return-query", "1"); document.body.classList.add("page-leaving"); setTimeout(() => { window.location.href = "/?query=1"; }, 360); });

function svgForExport() {
  const source = $("#riskGraph").cloneNode(true);
  source.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  source.setAttribute("width", "1200"); source.setAttribute("height", "744");
  const style = document.createElementNS("http://www.w3.org/2000/svg", "style");
  style.textContent = `.graph-edge{stroke:#66b69a;stroke-opacity:.82;stroke-linecap:round}.graph-edge.guarantee{stroke:#e4b05f}.graph-edge.equity{stroke:#9b83eb}.graph-edge.investment{stroke:#47c6b4}.graph-edge.trade{stroke:#e89552}.graph-edge.risk{stroke:#ec6571;stroke-dasharray:8 5}.edge-label-wrap rect{fill:#3a0a10;stroke:rgba(255,255,255,.35);stroke-width:1}.edge-label{fill:#fff7ef;font-size:12px;font-weight:700}.graph-node .node-halo{opacity:.24}.graph-node .node-main{stroke:#fff7ef;stroke-width:3}.graph-node text{fill:#fff;font-size:12px;font-weight:700;text-anchor:middle;paint-order:stroke;stroke:#3b060b;stroke-width:4px}`;
  source.prepend(style);
  const bg = document.createElementNS("http://www.w3.org/2000/svg", "rect"); bg.setAttribute("x", "0"); bg.setAttribute("y", "0"); bg.setAttribute("width", "1000"); bg.setAttribute("height", "620"); bg.setAttribute("fill", "#661017"); source.insertBefore(bg, source.children[1]);
  return new XMLSerializer().serializeToString(source);
}
$("#exportSvg").addEventListener("click", () => {
  if (!state.graph) return;
  downloadBlob(new Blob([svgForExport()], { type: "image/svg+xml;charset=utf-8" }), `${safeFilename()}_关联风险图谱.svg`);
});
$("#exportPng").addEventListener("click", () => {
  if (!state.graph) return;
  const svgText = svgForExport(); const blob = new Blob([svgText], { type: "image/svg+xml;charset=utf-8" }); const url = URL.createObjectURL(blob); const image = new Image();
  image.onload = () => {
    const canvas = document.createElement("canvas"); canvas.width = 1600; canvas.height = 992; const ctx = canvas.getContext("2d"); ctx.drawImage(image, 0, 0, canvas.width, canvas.height); URL.revokeObjectURL(url); canvas.toBlob((png) => downloadBlob(png, `${safeFilename()}_关联风险图谱.png`), "image/png");
  };
  image.src = url;
});


function initPremiumReportNavigation() {
  const nav = $("#reportProgressNav");
  const progress = $("#reportReadingProgress");
  if (!nav || !progress) return;
  const buttons = $$('[data-report-target]', nav);
  const sections = buttons.map((button) => document.getElementById(button.dataset.reportTarget)).filter(Boolean);
  buttons.forEach((button) => button.addEventListener('click', () => {
    document.getElementById(button.dataset.reportTarget)?.scrollIntoView({ behavior: 'smooth', block: 'start', inline: 'nearest' });
  }));
  const update = () => {
    if ($('#reportArea')?.hidden) return;
    const viewportMiddle = window.scrollY + window.innerHeight * .34;
    let active = sections[0];
    sections.forEach((section) => { if (section.offsetTop <= viewportMiddle) active = section; });
    buttons.forEach((button) => button.classList.toggle('active', button.dataset.reportTarget === active?.id));
    const start = sections[0]?.offsetTop || 0;
    const end = (sections.at(-1)?.offsetTop || start) + (sections.at(-1)?.offsetHeight || 1) - window.innerHeight;
    const ratio = Math.max(0, Math.min(1, (window.scrollY - start) / Math.max(1, end - start)));
    progress.style.transform = `scaleX(${ratio})`;
  };
  window.addEventListener('scroll', update, { passive: true });
  window.addEventListener('resize', update, { passive: true });
  update();
}

function bindPremiumMotionItems() {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  $$('.metric-card,.info-card,.quiet-action,.blue-button').forEach((item) => {
    if (item.dataset.motionBound === '1') return;
    item.dataset.motionBound = '1';
    item.addEventListener('pointermove', (event) => {
      const rect = item.getBoundingClientRect();
      const rx = ((event.clientY - rect.top) / rect.height - .5) * -3;
      const ry = ((event.clientX - rect.left) / rect.width - .5) * 4;
      item.style.transform = `perspective(800px) rotateX(${rx}deg) rotateY(${ry}deg) translateY(-2px)`;
    });
    item.addEventListener('pointerleave', () => { item.style.transform = ''; });
  });
}

function initPremiumPointerMotion() {
  const report = $('#reportArea');
  if (!report || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  report.addEventListener('pointermove', (event) => {
    const rect = report.getBoundingClientRect();
    report.style.setProperty('--pointer-x', `${event.clientX - rect.left}px`);
    report.style.setProperty('--pointer-y', `${event.clientY - rect.top}px`);
  }, { passive: true });
  bindPremiumMotionItems();
}

document.addEventListener('DOMContentLoaded', () => {
  initPremiumReportNavigation();
  initPremiumPointerMotion();
});


function historyRiskClass(level = "") {
  if (/重大|极高/.test(level)) return "critical";
  if (/高/.test(level)) return "high";
  if (/中|重点/.test(level)) return "medium";
  return "low";
}
async function loadHistoryDrawer() {
  const list = $("#historyList");
  if (!list) return;
  list.innerHTML = '<p class="history-empty">正在读取历史记录…</p>';
  try {
    const data = await fetchJSON("/api/history?limit=30");
    const items = data.items || [];
    list.innerHTML = items.length ? items.map((item) => `<article class="history-item" data-report-id="${escapeHTML(item.report_id)}">
      <button type="button" class="history-open"><span><strong>${escapeHTML(item.company_name || item.query)}</strong><small>${escapeHTML(item.stock_code || item.credit_code || item.query || "")}</small></span><em class="${historyRiskClass(item.risk_level)}">${escapeHTML(item.risk_level || "已完成")}</em></button>
      <time>${escapeHTML(item.created_at || "")}</time>
    </article>`).join("") : '<p class="history-empty">暂无历史查询。</p>';
    $$(".history-open", list).forEach((button) => button.addEventListener("click", () => {
      const id = button.closest(".history-item").dataset.reportId;
      document.body.classList.add("page-leaving");
      setTimeout(() => { window.location.href = `/report?report_id=${encodeURIComponent(id)}`; }, 320);
    }));
  } catch (error) {
    list.innerHTML = `<p class="history-empty">历史记录读取失败：${escapeHTML(error.message)}</p>`;
  }
}
function openHistoryDrawer() {
  const drawer = $("#historyDrawer");
  drawer?.classList.add("is-open");
  drawer?.setAttribute("aria-hidden", "false");
  document.body.classList.add("history-open");
  loadHistoryDrawer();
}
function closeHistoryDrawer() {
  const drawer = $("#historyDrawer");
  drawer?.classList.remove("is-open");
  drawer?.setAttribute("aria-hidden", "true");
  document.body.classList.remove("history-open");
}
$$('[data-history-open]').forEach((button) => button.addEventListener("click", openHistoryDrawer));
$$('[data-history-close]').forEach((button) => button.addEventListener("click", closeHistoryDrawer));
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeHistoryDrawer(); });

function showReportLoadError(message) {
  const veil = $("#reportLoadingVeil");
  if (!veil) return;
  veil.innerHTML = `<div class="report-load-error"><strong>报告打开失败</strong><p>${escapeHTML(message)}</p><a href="/?query=1">返回查询</a></div>`;
}
async function loadReportFromLocation() {
  const id = new URLSearchParams(window.location.search).get("report_id") || sessionStorage.getItem("chain-last-report");
  if (!id) {
    showReportLoadError("缺少报告编号，请返回查询页重新选择企业或从历史查询中打开报告。");
    return;
  }
  try {
    let report;
    try { report = await fetchJSON(`/api/reports/${encodeURIComponent(id)}`); }
    catch (_) { report = await fetchJSON(`/api/history/${encodeURIComponent(id)}`); }
    state.report = report;
    renderReport(report);
    document.title = `${report.company?.short_name || report.company?.name || "企业"}｜链网E判完整报告`;
    $("#modelLabel").textContent = report.model?.model_label || "DeepSeek V4 + Tavily";
    const veil = $("#reportLoadingVeil");
    requestAnimationFrame(() => {
      document.body.classList.add("report-ready");
      veil?.classList.add("is-hidden");
      setTimeout(() => {
        veil?.remove();
        activateReportMotion();
      }, 420);
    });
  } catch (error) {
    showReportLoadError(error.message || "报告不存在或已被清理。");
  }
}

document.addEventListener("DOMContentLoaded", loadReportFromLocation);
