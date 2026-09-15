/* 调试平台前端逻辑 */
"use strict";

const $ = (s) => document.querySelector(s);
const state = {
  traces: [],          // trace summary list
  currentTraceId: null,
  detailCache: {},
};

const KIND_COLOR = {
  root: "#3b82f6", agent: "#8b5cf6", thinking: "#a855f7", text: "#16a34a",
  tool: "#f59e0b", tool_exec: "#fb923c", tool_args: "#fbbf24", event: "#64748b", error: "#ef4444",
};
const KIND_LABEL = {
  root: "请求 root", agent: "Agent reply_stream", thinking: "模型思考",
  text: "生成回复", tool: "工具调用", tool_exec: "工具执行回填",
  tool_args: "生成参数", event: "事件",
};

function fmtMs(v) {
  if (v == null) return "-";
  const n = Number(v);
  return n >= 1000 ? (n / 1000).toFixed(2) + " s" : n.toFixed(1) + " ms";
}
function escapeHtml(x) {
  return String(x ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2200);
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

/* ---------------- Tab 切换 ---------------- */
function bindTabs() {
  $("#nav").addEventListener("click", (ev) => {
    const a = ev.target.closest("a[data-tab]");
    if (!a) return;
    document.querySelectorAll("#nav a").forEach((x) => x.classList.remove("active"));
    a.classList.add("active");
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    $("#panel-" + a.dataset.tab).classList.add("active");
  });
}

/* ---------------- 概览 ---------------- */
async function loadDashboard() {
  const d = await api("/api/debug/summary");
  const cards = [
    ["Trace 总数", d.total, ""],
    ["成功", d.ok, "done"],
    ["失败", d.failed, d.failed ? "pending" : ""],
    ["进行中", d.inflight, d.inflight ? "pending" : ""],
    ["平均总耗时", fmtMs(d.avg_total_ms), ""],
    ["平均 TTFT", fmtMs(d.avg_ttft_ms), ""],
    ["推理轮次合计", d.rounds_total, ""],
  ];
  $("#statGrid").innerHTML = cards.map(([lbl, num, cls]) =>
    `<div class="stat ${cls}"><div class="num">${num}</div><div class="lbl">${lbl}</div></div>`
  ).join("");

  const rows = Object.entries(d.tools || {}).map(([name, s]) => `
    <tr>
      <td><span class="kchip" style="background:var(--c-tool)">${escapeHtml(name)}</span></td>
      <td>${s.count}</td>
      <td>${fmtMs(s.total_ms)}</td>
      <td>${fmtMs(s.count ? s.total_ms / s.count : 0)}</td>
      <td>${fmtMs(s.max_ms)}</td>
      <td>${s.errors ? '<span style="color:var(--danger)">' + s.errors + "</span>" : s.errors}</td>
    </tr>`).join("");
  $("#toolBody").innerHTML = rows || '<tr><td colspan="6" class="muted">暂无数据，先去跟客服聊聊吧</td></tr>';
}

/* ---------------- Trace 列表 ---------------- */
function traceRowHtml(t, sel) {
  const tools = (t.tools || []).map((n) => `<span class="tool">${escapeHtml(n)}</span>`).join("");
  return `
  <div class="trace-item ${sel ? "sel" : ""}" data-id="${t.id}">
    <span class="id">${t.id.slice(4, 12)}</span>
    <span class="st ${t.status}">${t.status === "done" ? "完成" : t.status === "failed" ? "失败" : "进行中"}</span>
    <span class="msg" title="${escapeHtml(t.message)}">${escapeHtml(t.message)}</span>
    <span class="trace-tools">${tools}</span>
    <span class="dur">${fmtMs(t.total_ms)}</span>
    <span class="meta">${(t.started_at || "").slice(11, 19)}</span>
  </div>`;
}

function optionHtml(t) {
  const label = `[${t.id.slice(4, 12)}] ${t.message.slice(0, 26)}${t.message.length > 26 ? "…" : ""} (${fmtMs(t.total_ms)})`;
  return `<option value="${t.id}">${escapeHtml(label)}</option>`;
}

async function loadTraces(selectFirst) {
  const q = $("#traceSearch").value.trim();
  const data = await api("/api/debug/traces?limit=100" + (q ? "&q=" + encodeURIComponent(q) : ""));
  state.traces = data.traces || [];
  $("#traceCount").textContent = "共 " + state.traces.length + " 条";
  $("#traceList").innerHTML = state.traces.length
    ? state.traces.map((t) => traceRowHtml(t, t.id === state.currentTraceId)).join("")
    : '<div class="empty">暂无 trace，请先在客服聊天页发送一条消息</div>';

  // 同步三个下拉
  const opts = state.traces.map(optionHtml).join("");
  for (const id of ["#callTraceSelect", "#flameTraceSelect", "#wfTraceSelect"]) {
    const el = $(id);
    const prev = el.value || state.currentTraceId || (state.traces[0]?.id ?? "");
    el.innerHTML = opts;
    if (state.traces.some((t) => t.id === prev)) el.value = prev;
  }
  if (selectFirst && state.traces[0]) selectTrace(state.traces[0].id);
}

async function selectTrace(traceId) {
  state.currentTraceId = traceId;
  for (const id of ["#callTraceSelect", "#flameTraceSelect", "#wfTraceSelect"]) {
    $(id).value = traceId;
  }
  await refreshDetailViews();
}

async function getDetail(traceId) {
  if (!state.detailCache[traceId]) {
    const d = await api("/api/debug/traces/" + traceId);
    state.detailCache[traceId] = d.trace;
  }
  return state.detailCache[traceId];
}

async function refreshDetailViews() {
  if (!state.currentTraceId) return;
  const t = await getDetail(state.currentTraceId);
  renderCallTree(t);
  renderWaterfall(t);
  renderFlame();
  // 列表高亮
  document.querySelectorAll(".trace-item").forEach((el) =>
    el.classList.toggle("sel", el.dataset.id === state.currentTraceId));
}

/* ---------------- 调用树 ---------------- */
function spanNodeHtml(n) {
  const total = state._tree_total || n.value || 1;
  const pct = Math.min(100, (n.value / total) * 100);
  const kids = (n.children || []);
  const hasKids = kids.length > 0;
  const hasDetail = n.detail && Object.keys(n.detail).length > 0;
  const color = KIND_COLOR[n.kind] || "#64748b";
  const detailTxt = hasDetail
    ? Object.entries(n.detail).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join("\n")
    : "";
  return `
  <div class="tn ${n.status === "error" ? "error" : ""}" data-kind="${n.kind}">
    <span class="tree-toggle">${hasKids ? "▾" : ""}</span>
    <span class="tw">${fmtMs(n.value)}</span>
    <span class="kchip" style="background:${color}">${KIND_LABEL[n.kind] || n.kind}</span>
    <span>${escapeHtml(n.name)}</span>
    ${hasDetail ? '<span class="di-toggle" style="color:var(--brand);cursor:pointer">ⓘ</span>' : ""}
    <span class="bar" style="width:${pct}%;background:${color}"></span>
    <span class="dur">${pct.toFixed(0)}%</span>
    ${hasDetail ? '<div class="detail">' + escapeHtml(detailTxt) + "</div>" : ""}
  </div>
  ${hasKids ? '<div class="children">' + kids.map(spanNodeHtml).join("") + "</div>" : ""}`;
}

function renderCallTree(t) {
  state._tree_total = Math.max(1, t.total_ms || 1);
  const flame = state._flameSingleForTree;
  // 直接用 trace.spans 建树，保证即使 flame 端点未调也能渲染
  const root = buildSpanTreeFromTrace(t);
  const evs = (t.events || []).map((e) => `
    <div class="ev"><span class="tw">${fmtMs(e.at_ms)}</span>⚡ ${escapeHtml(e.note)}</div>
  `).join("");
  $("#callTree").innerHTML =
    spanNodeHtml(root) + (evs ? '<div style="margin-top:12px;border-top:1px dashed #eee;padding-top:8px">' + evs + "</div>" : "");
}

/* ---------------- 火焰图 ---------------- */
async function renderFlame() {
  const mode = $("#flameMode").value;
  const box = $("#flameBox");
  try {
    const q = new URLSearchParams();
    if (mode === "single" && state.currentTraceId) q.set("trace_id", state.currentTraceId);
    else q.set("limit", $("#flameLimit").value);
    const data = await api("/api/debug/flamegraph?" + q.toString());
    drawFlame(data.flame);
  } catch (e) {
    box.innerHTML = '<div class="empty">加载失败</div>';
  }
}

function drawFlame(root) {
  const box = $("#flameBox");
  const legend = $("#flameLegend");
  const rows = [];
  const seenKinds = new Set();

  function walk(node, depth, leftFrac, widthFrac) {
    if (widthFrac <= 0) return;
    while (rows.length <= depth) rows.push([]);
    rows[depth].push({ node, leftFrac, widthFrac });
    seenKinds.add(node.kind);
    if (!node.children || node.children.length === 0) return;
    const total = node.children.reduce((s, c) => s + Math.max(c.value, 0.1), 0);
    let x = leftFrac;
    for (const c of node.children) {
      const wf = widthFrac * (Math.max(c.value, 0.1) / total);
      walk(c, depth + 1, x, wf);
      x += wf;
    }
  }
  walk(root, 0, 0, 1);

  const html = rows.map((row, i) => {
    const segs = row.map(({ node, leftFrac, widthFrac }) => {
      const pct = (widthFrac * 100).toFixed(3);
      const color = KIND_COLOR[node.kind] || "#64748b";
      const pctOfRoot = root.value ? (node.value / root.value * 100).toFixed(1) : 0;
      const label = widthFrac > 0.04 ? escapeHtml(node.name.slice(0, 32)) : "";
      return `<div class="flame-node" style="width:${pct}%;background:${color}"
        data-n="${escapeHtml(node.name)}" data-k="${node.kind}" data-v="${node.value}"
        data-c="${node.count || 0}" data-p="${pctOfRoot}">${label}</div>`;
    }).join("");
    return `<div class="flame-row">${segs}</div>`;
  }).join("");

  box.innerHTML = html || '<div class="empty">暂无数据</div>';
  legend.innerHTML = [...seenKinds].map((k) =>
    `<span><span class="sw" style="background:${KIND_COLOR[k] || "#64748b"}"></span>${KIND_LABEL[k] || k}</span>`
  ).join("");
}

/* ---------------- 瀑布 ---------------- */
function renderWaterfall(t) {
  const total = Math.max(1, t.total_ms || 1);
  const box = $("#wfBox");
  const rows = [];
  // 网格刻度
  const ticks = [0.25, 0.5, 0.75].map((f) =>
    `<span style="position:absolute;left:${f * 100}%;top:0;bottom:0;border-left:1px dashed #dfe3ec"></span>`
  ).join("");
  rows.push(`<div class="wf-grid" style="position:relative;height:14px">${ticks}</div>`);

  // span rows
  for (const sp of t.spans.slice().sort((a, b) => a.start_ms - b.start_ms || (b.end_ms || 0) - (a.end_ms || 0))) {
    const left = (sp.start_ms / total) * 100;
    const width = Math.max(0.4, ((sp.duration_ms) / total) * 100);
    const color = KIND_COLOR[sp.kind] || "#64748b";
    const err = sp.status === "error";
    rows.push(`
      <div class="wf-row" title="${escapeHtml(sp.name)} · ${fmtMs(sp.duration_ms)}">
        <span class="kchip" style="background:${color};min-width:74px;text-align:center">${KIND_LABEL[sp.kind] || sp.kind}</span>
        <div class="wf-lane">
          <div class="wf-bar ${err ? "err" : ""}" style="left:${left}%;width:${Math.min(width, 100 - left)}%;background:${err ? "" : color}">
            ${escapeHtml(sp.name.slice(0, 22))} · ${fmtMs(sp.duration_ms)}
          </div>
        </div>
      </div>`);
  }
  // events
  for (const e of (t.events || [])) {
    const left = (e.at_ms / total) * 100;
    rows.push(`
      <div class="wf-ev" title="${escapeHtml(e.note)}">
        <span style="min-width:74px;text-align:center;color:#a855f7">⚡</span>
        <div class="wf-lane">
          <span style="position:absolute;left:${left}%;top:9px;width:6px;height:6px;background:#a855f7;border-radius:50%"></span>
          <span style="position:absolute;left:calc(${left}% + 12px);top:3px;font-size:11px;color:#5a6172">${escapeHtml(e.note.slice(0, 40))}</span>
        </div>
      </div>`);
  }
  box.innerHTML = rows.join("") || '<div class="empty">暂无数据</div>';
}

/* ---------------- 建树工具（不上依赖 flame 端点） ---------------- */
function buildSpanTreeFromTrace(t) {
  const nodes = {};
  for (const sp of t.spans) {
    nodes[sp.id] = {
      id: sp.id, kind: sp.kind, name: sp.name, parent: sp.parent,
      value: sp.duration_ms, status: sp.status, detail: sp.detail || {}, children: [],
    };
  }
  const root = {
    id: "__root__", kind: "root", name: "处理消息 chat", parent: null,
    value: t.total_ms, status: t.status, detail: {
      message: t.message, model: t.model_name, status: t.status,
      rounds: t.rounds, ttft_ms: t.ttft_ms, tools: (t.tools || []).join(","),
    }, children: [],
  };
  for (const sp of t.spans) {
    const n = nodes[sp.id];
    (sp.parent && nodes[sp.parent] ? nodes[sp.parent] : root).children.push(n);
  }
  return root;
}

/* ---------------- 事件绑定 ---------------- */
function bindEvents() {
  $("#refreshTraces").addEventListener("click", () => loadTraces(false));
  $("#traceSearch").addEventListener("input", () => loadTraces(false));
  $("#clearTraces").addEventListener("click", async () => {
    if (!confirm("确定清空所有 trace 记录？")) return;
    await api("/api/debug/clear", { method: "POST" });
    state.currentTraceId = null;
    state.detailCache = {};
    await loadTraces(false);
    toast("已清空");
  });
  $("#traceList").addEventListener("click", (ev) => {
    const item = ev.target.closest(".trace-item");
    if (item) selectTrace(item.dataset.id);
  });
  for (const id of ["#callTraceSelect", "#flameTraceSelect", "#wfTraceSelect"]) {
    $(id).addEventListener("change", (ev) => selectTrace(ev.target.value));
  }
  $("#flameMode").addEventListener("change", renderFlame);
  $("#flameLimit").addEventListener("change", renderFlame);

  // 调用树伸缩 / 详情
  $("#callTree").addEventListener("click", (ev) => {
    const tn = ev.target.closest(".tn");
    if (!tn) return;
    if (ev.target.closest(".di-toggle")) { tn.classList.toggle("open"); return; }
    if (ev.target.closest(".tree-toggle")) {
      const kids = tn.nextElementSibling;
      if (kids && kids.classList.contains("children")) kids.style.display = kids.style.display === "none" ? "" : "none";
    }
  });

  // 火焰 tooltip
  const tip = $("#flameTip");
  $("#flameBox").addEventListener("mousemove", (ev) => {
    const node = ev.target.closest(".flame-node");
    if (!node) { tip.classList.add("hidden"); return; }
    tip.innerHTML =
      `<div style="font-weight:600">${escapeHtml(node.dataset.n)}</div>
       <div>类型：${KIND_LABEL[node.dataset.k] || node.dataset.k}</div>
       <div>累计耗时：${fmtMs(node.dataset.v)}（占 ${node.dataset.p}%）</div>
       <div>发生次数：${node.dataset.c}</div>`;
    tip.classList.remove("hidden");
    const pad = 14;
    let x = ev.clientX + pad, y = ev.clientY + pad;
    const r = tip.getBoundingClientRect();
    if (x + r.width > window.innerWidth - 10) x = ev.clientX - r.width - pad;
    if (y + r.height > window.innerHeight - 10) y = ev.clientY - r.height - pad;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  });
  $("#flameBox").addEventListener("mouseleave", () => tip.classList.add("hidden"));
}

/* ---------------- 启动 ---------------- */
async function boot() {
  bindTabs();
  bindEvents();
  try {
    await loadTraces(true);
    await loadDashboard();
  } catch (e) {
    toast("加载失败：" + e.message);
  }
}
document.addEventListener("DOMContentLoaded", boot);
