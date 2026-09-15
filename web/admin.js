/* 云雀商城 · 客服管理后台逻辑 */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let currentSession = null;
let allTickets = [];
let editingFaqId = null;

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove("show"), 2200);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = "请求失败";
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

async function refreshStats() {
  const s = await api("/api/admin/stats");
  $("#statGrid").innerHTML = [
    ["当前会话", s.sessions, ""],
    ["已转人工", s.handed_off, ""],
    ["工单总数", s.tickets, ""],
    ["待处理工单", s.tickets_pending, "pending"],
    ["已完成工单", s.tickets_done, "done"],
    ["订单数", s.orders, ""],
    ["用户数", s.users, ""]
  ].map(([l, n, cls]) =>
    '<div class="stat ' + cls + '"><div class="num">' + n + '</div><div class="lbl">' + l + '</div></div>'
  ).join("");
}

/* ---------------- 会话 ---------------- */
async function refreshSessions() {
  const data = await api("/api/admin/sessions");
  const sessions = data.sessions || [];
  $("#sessionsCount").textContent = "共 " + sessions.length + " 个会话";
  $("#sessionList").innerHTML = sessions.map((s) =>
    '<div class="session-item ' + (s.session_id === currentSession ? "active" : "") + '" data-sid="' + s.session_id + '">' +
      '<div class="preview">' + esc(s.preview || "（新会话）") + '</div>' +
      '<div class="meta">' + s.session_id + " · " + s.message_count + " 条消息" + (s.handed_off ? " · 🟣 已转人工" : "") + "</div>" +
    "</div>"
  ).join("");
  Array.from(document.querySelectorAll(".session-item")).forEach((el) =>
    el.addEventListener("click", () => { currentSession = el.getAttribute("data-sid"); loadSession(el.getAttribute("data-sid")); })
  );
  if (currentSession) loadSession(currentSession);
}

async function loadSession(sid) {
  const data = await api("/api/admin/sessions/" + sid + "/history");
  const messages = data.messages || [];
  Array.from(document.querySelectorAll(".session-item")).forEach((e) =>
    e.classList.toggle("active", e.getAttribute("data-sid") === sid)
  );
  const box = $("#sessionDetail");
  if (!messages.length) { box.innerHTML = '<div class="empty">暂无消息</div>'; return; }
  box.innerHTML = messages.map((m) => {
    let extra = "";
    if (m.thinking) extra += '<div class="thinking">💭 ' + esc(m.thinking) + "</div>";
    (m.tool_calls || []).forEach((tc) => {
      extra += '<div class="tool">🔧 <b>' + esc(tc.name) + "</b> 参数：" + esc(tc.args) +
        (tc.result ? "<div>→ " + esc(tc.result) + "</div>" : "") + "</div>";
    });
    return '<div class="msg ' + m.role + '"><div class="who">' + (m.role === "user" ? "顾客" : "小云") + "</div>" +
      '<div class="body">' + esc(m.text || "") + extra + "</div></div>";
  }).join("");
}

/* ---------------- 工单 ---------------- */
async function refreshTickets() {
  const data = await api("/api/admin/tickets");
  allTickets = data.tickets || [];
  renderTickets();
}

function renderTickets() {
  const f = $("#ticketFilter").value;
  const list = f ? allTickets.filter((t) => t.status === f) : allTickets;
  const statusTag = { "待处理": "pending", "处理中": "processing", "已完成": "done", "已转人工": "human" };
  $("#ticketBody").innerHTML = list.map((t) =>
    "<tr>" +
      "<td>" + esc(t.ticket_id) + "</td>" +
      "<td>" + esc(t.title) + "</td>" +
      '<td><span class="tag">' + esc(t.category_cn || t.category) + "</span></td>" +
      '<td><span class="tag ' + (t.priority === "high" ? "high" : "") + '">' + esc(t.priority) + "</span></td>" +
      '<td><span class="tag ' + (statusTag[t.status] || "") + '">' + esc(t.status) + "</span></td>" +
      "<td>" + esc(t.assignee || "—") + "</td>" +
      "<td>" + esc(t.created_at || "") + "</td>" +
      '<td><button class="btn sm" data-id="' + t.ticket_id + '">处理</button></td>' +
    "</tr>"
  ).join("");
  Array.from(document.querySelectorAll("#ticketBody .btn")).forEach((b) =>
    b.addEventListener("click", () => openTicket(b.getAttribute("data-id")))
  );
}

function openTicket(id) {
  const t = allTickets.find((x) => x.ticket_id === id);
  if (!t) return;
  $("#ticketModalTitle").textContent = "处理工单 · " + t.ticket_id;
  $("#ticketModalBody").innerHTML =
    '<div class="kv"><b>类型：</b>' + esc(t.category_cn || t.category) + "</div>" +
    '<div class="kv"><b>标题：</b>' + esc(t.title) + "</div>" +
    '<div class="kv"><b>详情：</b>' + esc(t.detail) + "</div>" +
    '<div class="kv"><b>联系方式：</b>' + esc(t.contact || "—") + "　<b>订单：</b>" + esc(t.order_id || "—") + "</div>" +
    '<div class="kv"><b>创建：</b>' + esc(t.created_at || "") + "　<b>优先级：</b>" + esc(t.priority) + "</div>" +
    (t.reply ? '<div class="kv"><b>回复内容：</b>' + esc(t.reply) + "</div>" : "") +
    '<div class="form" style="margin-top:12px">' +
      "<label>状态<select id=\"ticketStatus\">" +
        ["待处理", "处理中", "已完成", "已转人工"].map((s) => "<option " + (s === t.status ? "selected" : "") + ">" + s + "</option>").join("") +
      "</select></label>" +
      '<label>受理人 <input id="ticketAssignee" value="' + esc(t.assignee || "") + '"></label>' +
      '<label>回复内容 <textarea id="ticketReply" rows="3">' + esc(t.reply || "") + "</textarea></label>" +
    "</div>";
  $("#ticketModal")._tid = t.ticket_id;
  $("#ticketModal").classList.remove("hidden");
}

async function saveTicket() {
  const id = $("#ticketModal")._tid;
  const body = {
    status: $("#ticketStatus").value,
    assignee: $("#ticketAssignee").value.trim(),
    reply: $("#ticketReply").value.trim()
  };
  try {
    await api("/api/admin/tickets/" + id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("#ticketModal").classList.add("hidden");
    toast("工单已更新");
    refreshTickets();
  } catch (e) { toast(e.message); }
}

/* ---------------- FAQ ---------------- */
async function refreshFaq() {
  const data = await api("/api/admin/faq");
  const entries = data.entries || [];
  window.__faqList = entries;
  $("#faqBody").innerHTML = entries.map((e) =>
    "<tr>" +
      "<td>" + esc(e.title) + "</td>" +
      "<td>" + esc((e.keywords || []).join("、")) + "</td>" +
      "<td>" + esc(e.content) + "</td>" +
      '<td style="white-space:nowrap">' +
        '<button class="btn sm" data-act="edit" data-id="' + e.id + '">编辑</button> ' +
        '<button class="btn sm danger" data-act="del" data-id="' + e.id + '">删除</button>' +
      "</td>" +
    "</tr>"
  ).join("");
  Array.from(document.querySelectorAll("#faqBody [data-act]")).forEach((b) =>
    b.addEventListener("click", () => {
      if (b.getAttribute("data-act") === "edit") openFaqModal(b.getAttribute("data-id"));
      else deleteFaq(b.getAttribute("data-id"));
    })
  );
}

async function deleteFaq(id) {
  if (!confirm("确定删除该 FAQ 条目？")) return;
  try {
    await api("/api/admin/faq/" + id, { method: "DELETE" });
    toast("已删除");
    refreshFaq();
  } catch (e) { toast(e.message); }
}

function openFaqModal(id) {
  editingFaqId = id || null;
  $("#faqModalTitle").textContent = id ? "编辑 FAQ" : "新增 FAQ";
  const e = id ? (window.__faqList || []).find((x) => x.id === id) : null;
  $("#faqFormTitle").value = e ? e.title : "";
  $("#faqFormKeywords").value = e ? (e.keywords || []).join(",") : "";
  $("#faqFormContent").value = e ? e.content : "";
  $("#faqModal").classList.remove("hidden");
}

async function saveFaq() {
  const title = $("#faqFormTitle").value.trim();
  const content = $("#faqFormContent").value.trim();
  if (!title || !content) { toast("标题与内容不能为空"); return; }
  const keywords = $("#faqFormKeywords").value.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
  const body = JSON.stringify({ title, keywords, content });
  try {
    if (editingFaqId) await api("/api/admin/faq/" + editingFaqId, { method: "PUT", headers: { "Content-Type": "application/json" }, body });
    else await api("/api/admin/faq", { method: "POST", headers: { "Content-Type": "application/json" }, body });
    $("#faqModal").classList.add("hidden");
    toast("已保存");
    refreshFaq();
  } catch (e) { toast(e.message); }
}


/* ---------------- 商品 ---------------- */
let editingProductSku = null;

async function refreshProducts() {
  const data = await api("/api/admin/products");
  const list = data.products || [];
  window.__productList = list;
  $("#productBody").innerHTML = list.map((p) =>
    "<tr>" +
      "<td>" + esc(p.sku) + "</td>" +
      "<td>" + esc(p.name) + "</td>" +
      "<td>" + esc(p.category || "—") + "</td>" +
      "<td>￥" + Number(p.price || 0).toFixed(2) + "</td>" +
      "<td>" + (p.stock == null ? "—" : p.stock) + "</td>" +
      "<td>" + esc(p.spec || "—") + "</td>" +
      '<td style="white-space:nowrap">' +
        '<button class="btn sm" data-act="edit" data-id="' + p.sku + '">编辑</button> ' +
        '<button class="btn sm danger" data-act="del" data-id="' + p.sku + '">删除</button>' +
      "</td>" +
    "</tr>"
  ).join("");
  Array.from(document.querySelectorAll("#productBody .btn")).forEach((b) =>
    b.addEventListener("click", () => {
      if (b.getAttribute("data-act") === "edit") openProductModal(b.getAttribute("data-id"));
      else deleteProduct(b.getAttribute("data-id"));
    })
  );
}

async function deleteProduct(sku) {
  if (!confirm("确定删除该商品？")) return;
  try {
    await api("/api/admin/products/" + sku, { method: "DELETE" });
    toast("已删除");
    refreshProducts();
    refreshStats();
  } catch (e) { toast(e.message); }
}

function openProductModal(sku) {
  editingProductSku = sku || null;
  $("#productModalTitle").textContent = sku ? "编辑商品" : "新增商品";
  const p = sku ? (window.__productList || []).find((x) => x.sku === sku) : null;
  $("#productFormName").value = p ? p.name : "";
  $("#productFormCategory").value = p ? (p.category || "") : "";
  $("#productFormPrice").value = p ? p.price : "";
  $("#productFormStock").value = p ? p.stock : "";
  $("#productFormSpec").value = p ? (p.spec || "") : "";
  $("#productFormDesc").value = p ? (p.desc || "") : "";
  $("#productModal").classList.remove("hidden");
}

async function saveProduct() {
  const name = $("#productFormName").value.trim();
  const price = Number($("#productFormPrice").value);
  if (!name || isNaN(price) || price <= 0) { toast("名称与价格必填且价格需大于 0"); return; }
  const body = JSON.stringify({
    name,
    price,
    category: $("#productFormCategory").value.trim(),
    stock: parseInt($("#productFormStock").value || "0", 10) || 0,
    spec: $("#productFormSpec").value.trim(),
    desc: $("#productFormDesc").value.trim()
  });
  try {
    if (editingProductSku) await api("/api/admin/products/" + editingProductSku, { method: "PUT", headers: { "Content-Type": "application/json" }, body });
    else await api("/api/admin/products", { method: "POST", headers: { "Content-Type": "application/json" }, body });
    $("#productModal").classList.add("hidden");
    toast("已保存");
    refreshProducts();
    refreshStats();
  } catch (e) { toast(e.message); }
}

/* ---------------- 订单 / 用户 ---------------- */
async function refreshOrders() {
  const data = await api("/api/admin/orders");
  $("#orderBody").innerHTML = (data.orders || []).map((o) =>
    "<tr>" +
      "<td>" + esc(o.order_id) + "</td>" +
      "<td>" + esc(o.phone_tail || "") + "</td>" +
      "<td>" + esc((o.items || []).map((i) => i.name + "×" + i.qty).join("、")) + "</td>" +
      "<td>￥" + Number(o.amount || 0).toFixed(2) + "</td>" +
      "<td>" + esc(o.status) + "</td>" +
      "<td>" + esc(o.created_at || "") + "</td>" +
    "</tr>"
  ).join("");
}

async function refreshUsers() {
  const data = await api("/api/admin/users");
  $("#userBody").innerHTML = (data.users || []).map((u) =>
    "<tr>" +
      "<td>" + esc(u.user_id) + "</td>" +
      "<td>" + esc(u.nickname) + "</td>" +
      "<td>" + esc(u.phone_tail || "") + "</td>" +
      "<td>" + esc(u.member_level || "—") + "</td>" +
      "<td>" + (u.points == null ? "—" : u.points) + "</td>" +
      "<td>" + esc((u.coupons || []).map((c) => c.title).join("、") || "—") + "</td>" +
    "</tr>"
  ).join("");
}

/* ---------------- 导航 ---------------- */
function switchTab(name) {
  Array.from(document.querySelectorAll(".nav a")).forEach((a) => a.classList.toggle("active", a.getAttribute("data-tab") === name));
  Array.from(document.querySelectorAll(".tab-panel")).forEach((p) => p.classList.toggle("active", p.id === "panel-" + name));
  return name;
}

const loaders = {
  overview: refreshStats,
  sessions: refreshSessions,
  tickets: refreshTickets,
  faq: refreshFaq,
  products: refreshProducts,
  orders: refreshOrders,
  users: refreshUsers
};

/* ---------------- 事件绑定 ---------------- */
document.querySelector("#nav").addEventListener("click", (e) => {
  const a = e.target.closest("a[data-tab]");
  if (!a) return;
  const name = switchTab(a.getAttribute("data-tab"));
  if (loaders[name]) loaders[name]().catch((err) => toast(err.message));
});

document.querySelector("#refreshSessions").addEventListener("click", () => refreshSessions().catch((err) => toast(err.message)));
document.querySelector("#refreshTickets").addEventListener("click", () => refreshTickets().catch((err) => toast(err.message)));
document.querySelector("#ticketFilter").addEventListener("change", renderTickets);
document.querySelector("#newFaq").addEventListener("click", () => openFaqModal());
document.querySelector("#faqModalCancel").addEventListener("click", () => document.querySelector("#faqModal").classList.add("hidden"));
document.querySelector("#faqModalSave").addEventListener("click", saveFaq);
document.querySelector("#newProduct").addEventListener("click", () => openProductModal());
document.querySelector("#productModalCancel").addEventListener("click", () => document.querySelector("#productModal").classList.add("hidden"));
document.querySelector("#productModalSave").addEventListener("click", saveProduct);
document.querySelector("#ticketModalCancel").addEventListener("click", () => document.querySelector("#ticketModal").classList.add("hidden"));
document.querySelector("#ticketModalSave").addEventListener("click", saveTicket);

/* ---------------- 启动 ---------------- */
(async () => {
  try { await refreshStats(); } catch (_) {}
  try { const d = await api("/api/admin/faq"); window.__faqList = d.entries || []; } catch (_) {}
  // 每 15 秒静默刷新概览
  setInterval(() => { refreshStats().catch(() => {}); }, 15000);
})();

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}