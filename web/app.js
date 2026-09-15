/* 云雀商城智能客服前端
 * 通过 SSE（POST /api/chat）接收 AgentScope 事件流：
 * thinking / delta / tool_call / tool_call_args / tool_result / done / error
 */
(function () {
  "use strict";

  const LS_KEY = "yk_cs_session";
  const state = {
    config: null,
    sessionId: null,
    streaming: false,
  };

  const $ = (id) => document.getElementById(id);
  const el = {
    messages: $("messages"),
    input: $("input"),
    sendBtn: $("send-btn"),
    newSession: $("new-session-btn"),
    quick: $("quick-prompts"),
    sessions: $("session-list"),
    agentName: $("agent-name"),
    agentAvatar: $("agent-avatar"),
    modelBadge: $("model-badge"),
    statusDot: $("status-dot"),
    statusText: $("status-text"),
  };

  // 工具名 → 友好展示（图标 + 中文名）
  const TOOL_META = {
    query_order: { icon: "📦", label: "查询订单" },
    list_recent_orders: { icon: "🧾", label: "最近订单" },
    track_logistics: { icon: "🚚", label: "物流跟踪" },
    search_faq: { icon: "📚", label: "知识库检索" },
    list_products: { icon: "🛍️", label: "商品列表" },
    query_product: { icon: "🔍", label: "商品详情" },
    view_cart: { icon: "🛒", label: "查看购物车" },
    add_to_cart: { icon: "➕", label: "加入购物车" },
    update_cart_item: { icon: "✏️", label: "更新购物车" },
    place_order: { icon: "📝", label: "提交订单" },
    pay_order: { icon: "💳", label: "订单支付" },
    query_user_profile: { icon: "👤", label: "查询会员" },
    create_ticket: { icon: "🎫", label: "创建工单" },
    transfer_to_human: { icon: "🙋", label: "转人工客服" },
    check_refund_policy: { icon: "📜", label: "退款政策" },
    apply_refund: { icon: "💰", label: "申请退款" },
    cancel_order: { icon: "🚫", label: "取消订单" },
  };

  function toolMeta(name) {
    return TOOL_META[name] || { icon: "🔧", label: "调用工具" };
  }

  // ---------------- 工具函数 ----------------
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // 极简 markdown：**加粗** + 换行（其余保持原样）
  function miniMarkdown(text) {
    const esc = escapeHtml(text);
    return esc
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\n/g, "<br/>");
  }

  function nowTime() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  async function api(url, opts) {
    const res = await fetch(url, opts);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  // ---------------- 渲染 ----------------
  function setStatus(online, text) {
    el.statusDot.classList.toggle("online", online);
    el.statusText.textContent = text || (online ? "在线" : "连接中…");
  }

  function addBubble(role, text, opts) {
    opts = opts || {};
    const wrap = document.createElement("div");
    wrap.className = `msg ${role}`;
    const avatarTxt = role === "assistant" ? escapeHtml(el.agentAvatar.textContent || "☁") : "我";
    wrap.innerHTML = `
      <div class="avatar">${avatarTxt}</div>
      <div class="msg-main">
        <div class="bubble ${opts.extraClass || ""}"></div>
        ${opts.time ? `<div class="time">${escapeHtml(opts.time)}</div>` : ""}
      </div>`;
    wrap.querySelector(".bubble").textContent = text;
    el.messages.appendChild(wrap);
    scrollBottom();
    return wrap.querySelector(".bubble");
  }

  function scrollBottom() {
    el.messages.scrollTop = el.messages.scrollHeight;
  }

  function addSystemLine(text) {
    const line = document.createElement("div");
    line.className = "stream-status";
    line.innerHTML = `<span class="spinner"></span><span>${escapeHtml(text)}</span>`;
    el.messages.appendChild(line);
    scrollBottom();
    return line;
  }

  function showThinking(slotEl, open) {
    if (slotEl.thinkingEl) return slotEl.thinkingEl;
    const wrap = document.createElement("div");
    wrap.className = "thinking";
    wrap.innerHTML = `<details${open === false ? "" : " open"}><summary>🧠 思考过程</summary><div class="body"></div></details>`;
    slotEl.main.appendChild(wrap);
    slotEl.thinkingEl = wrap.querySelector(".body");
    scrollBottom();
    return slotEl.thinkingEl;
  }

  function addToolCard(slotEl, id, name) {
    const meta = toolMeta(name);
    const card = document.createElement("div");
    card.className = "tool-card";
    card.innerHTML = `
      <div class="head" title="点击查看调用详情">
        <span class="ico">${meta.icon}</span>
        <span class="txt">
          <span class="lb">${escapeHtml(meta.label)}</span>
          <span class="nm">${escapeHtml(name)}</span>
        </span>
        <span class="st doing"><i></i>运行中</span>
        <span class="chev">›</span>
      </div>
      <div class="body">
        <div class="sec-label">请求参数</div>
        <pre class="args">（等待参数…）</pre>
        <div class="sec-label">返回结果</div>
        <pre class="result">（等待结果…）</pre>
      </div>`;
    card.querySelector(".head").addEventListener("click", () => {
      card.classList.toggle("open");
      scrollBottom();
    });
    slotEl.main.appendChild(card);
    slotEl.tools = slotEl.tools || {};
    slotEl.tools[id] = card;
    scrollBottom();
    return card;
  }

  function markToolDone(card) {
    const st = card.querySelector(".st");
    st.classList.remove("doing");
    st.classList.add("ok");
    st.textContent = "✓ 已完成";
  }

  function prettyArgs(raw) {
    let t = (raw || "").trim();
    if (!t) return "（无参数）";
    try { t = JSON.stringify(JSON.parse(t), null, 2); } catch (_) { /* 非 JSON */ }
    return t;
  }

  function prettyToolResult(raw) {
    if (!raw) return "(空结果)";
    let text = raw;
    try {
      const json = JSON.parse(raw);
      text = JSON.stringify(json, null, 2);
    } catch (_) { /* 非 JSON */ }
    return text.length > 1600 ? text.slice(0, 1600) + "…" : text;
  }

  function fillToolBody(card, argsRaw, resultRaw) {
    card.querySelector(".args").textContent = prettyArgs(argsRaw);
    card.querySelector(".result").textContent = prettyToolResult(resultRaw);
  }

  function createAssistantSlot() {
    const wrap = document.createElement("div");
    wrap.className = "msg assistant";
    const avatarTxt = el.agentAvatar.textContent || "☁";
    wrap.innerHTML = `
      <div class="avatar">${escapeHtml(avatarTxt)}</div>
      <div class="msg-main">
        <div class="bubble"><span class="cursor" id="typing-cursor"></span></div>
        <div class="time" hidden></div>
      </div>`;
    el.messages.appendChild(wrap);
    scrollBottom();
    return {
      main: wrap.querySelector(".msg-main"),
      bubble: wrap.querySelector(".bubble"),
      time: wrap.querySelector(".time"),
      text: "",
      tools: {},
      thinkingEl: null,
      buf: null,
    };
  }

  function renderBubble(slot) {
    let html = miniMarkdown(slot.text);
    if (state.streaming) html += '<span class="cursor"></span>';
    slot.bubble.innerHTML = html;
    scrollBottom();
  }
  // ---------------- SSE 解析 ----------------
  function send(message) {
    if (state.streaming) return;
    state.streaming = true;
    setSendDisabled(true);
    el.input.value = "";
    el.input.style.height = "auto";
    addBubble("user", message, { time: nowTime() });
    const slot = createAssistantSlot();
    const statusLine = addSystemLine(`${state.config.agent_name || "小云"}正在思考…`);

    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId, message }),
    })
      .then(async (res) => {
        if (!res.ok || !res.body) throw new Error("请求失败");
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const parts = buf.split("\n\n");
          buf = parts.pop();
          for (const part of parts) handleSsePart(part, slot, statusLine);
        }
        if (buf.trim()) handleSsePart(buf, slot, statusLine);
        finishReply(slot, statusLine);
      })
      .catch((err) => {
        finishReply(slot, statusLine, true);
        if (statusLine) statusLine.remove();
        renderBubble(slot);
        slot.bubble.innerHTML += '<br/><span class="err-line">（网络或服务异常，请稍后再试）</span>';
      })
      .finally(() => {
        state.streaming = false;
        setSendDisabled(false);
        refreshSessions();
      });

    function finishReply(s, line) {
      if (s.buf) s.buf.append(s.text);
      state.streaming = false;
      if (line) line.remove();
      const cursorEl = s.bubble.querySelector(".cursor");
      if (cursorEl) cursorEl.remove();
      if (s.time) {
        s.time.hidden = false;
        s.time.textContent = nowTime();
      }
      renderBubble(s);
    }
  }

  function handleSsePart(part, slot, statusLine) {
    const evtLine = /^event: (.+)$/m.exec(part);
    const dataLine = /^data: (.+)$/m.exec(part);
    const event = evtLine ? evtLine[1].trim() : "message";
    if (!dataLine) return;
    let data = {};
    try { data = JSON.parse(dataLine[1]); } catch (_) { return; }

    switch (event) {
      case "meta":
        if (data.session_id && !state.sessionId) state.sessionId = data.session_id;
        if (statusLine) statusLine.remove();
        break;
      case "thinking":
        if (data.delta) {
          const t = showThinking(slot);
          t.textContent += data.delta;
          scrollBottom();
        }
        break;
      case "delta":
        if (data.text) { slot.text += data.text; renderBubble(slot); }
        break;
      case "tool_call":
        if (data.id && data.name) addToolCard(slot, data.id, data.name);
        break;
      case "tool_call_args":
        if (data.id && slot.tools[data.id]) {
          slot.tools[data.id].dataset.args = data.args || "";
        }
        break;
      case "tool_result":
        if (data.id && slot.tools[data.id]) {
          const card = slot.tools[data.id];
          markToolDone(card);
          fillToolBody(card, card.dataset.args, data.result);
          scrollBottom();
        }
        break;
      case "done":
        break;
      case "error":
        slot.bubble.innerHTML += `<br/><span class="err-line">${escapeHtml(data.message || "出错了")}</span>`;
        break;
      case "hint":
        break;
      default:
        break;
    }
  }

  function setSendDisabled(v) {
    el.sendBtn.disabled = v;
  }

  // ---------------- 会话 ----------------
  async function createSession() {
    const r = await api("/api/sessions", { method: "POST" });
    state.sessionId = r.session_id;
    localStorage.setItem(LS_KEY, r.session_id);
    el.messages.innerHTML = "";
    showEmptyHint();
  }

  async function switchSession(id) {
    state.sessionId = id;
    localStorage.setItem(LS_KEY, id);
    const r = await api(`/api/sessions/${id}/history`);
    renderHistory(r.messages || []);
  }

  function showEmptyHint() {
    el.messages.innerHTML = `
      <div class="empty-hint">
        <div class="big">☁️</div>
        您好，我是<b>${escapeHtml(state.config.agent_name || "小云")}</b>，云雀商城智能客服。<br/>
        可以帮您<b>查订单 / 查物流</b>、<b>退款退货</b>、<b>取消订单</b>、<br/>
        解答<b>运费 / 发票 / 会员</b>等问题，或<b>转人工客服</b>。
      </div>`;
  }

  function renderHistory(messages) {
    el.messages.innerHTML = "";
    if (!messages.length) return showEmptyHint();
    for (const m of messages) {
      if (m.role === "user") {
        addBubble("user", m.text);
      } else {
        const slot = createAssistantSlot();
        slot.text = m.text || "";
        renderBubble(slot);
        if ((m.thinking || "").trim()) {
          const t = showThinking(slot, false);
          t.textContent = m.thinking;
        }
        for (const tc of m.tool_calls || []) {
          const card = addToolCard(slot, tc.id, tc.name);
          markToolDone(card);
          fillToolBody(card, tc.args || "", tc.result);
        }
      }
    }
    scrollBottom();
  }

  async function refreshSessions() {
    try {
      const r = await api("/api/sessions");
      el.sessions.innerHTML = "";
      for (const s of (r.sessions || [])) {
        const it = document.createElement("div");
        it.className = "session-item" + (s.session_id === state.sessionId ? " active" : "");
        const t = s.preview || "(空对话)";
        it.innerHTML = `<span class="t">${escapeHtml(t)}</span><span class="m">${escapeHtml((s.message_count || 0) + " 条消息")}</span>`;
        it.addEventListener("click", () => {
          if (s.session_id !== state.sessionId && !state.streaming) switchSession(s.session_id);
        });
        el.sessions.appendChild(it);
      }
    } catch (_) {}
  }

  // ---------------- 初始化 ----------------
  async function init() {
    try {
      state.config = await api("/api/config");
    } catch (_) {
      setStatus(false, "无法连接后端");
      return;
    }
    el.agentName.textContent = state.config.agent_name;
    el.agentAvatar.textContent = state.config.agent_name[0];
    const providerMap = { dashscope: "通义千问", openai: "OpenAI", mock: "离线演示" };
    el.modelBadge.textContent = `${providerMap[state.config.model_provider] || "AI"} · ${state.config.model_name}`;

    el.quick.innerHTML = "";
    for (const q of state.config.quick_prompts || []) {
      const chip = document.createElement("div");
      chip.className = "chip";
      chip.textContent = q;
      chip.addEventListener("click", () => {
        el.input.value = q;
        send(q);
      });
      el.quick.appendChild(chip);
    }

    // 恢复或新建会话
    const saved = localStorage.getItem(LS_KEY);
    try {
      await api(`/api/sessions/${saved}/history`);
      state.sessionId = saved;
      const r = await api(`/api/sessions/${saved}/history`);
      renderHistory(r.messages || []);
    } catch (_) {
      await createSession();
    }
    refreshSessions();
    setStatus(true, "在线");

    el.newSession.addEventListener("click", async () => {
      if (state.streaming) return;
      await createSession();
      await refreshSessions();
    });
    el.sendBtn.addEventListener("click", () => {
      const v = el.input.value.trim();
      if (v) send(v);
    });
    el.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        const v = el.input.value.trim();
        if (v) send(v);
      }
    });
    el.input.addEventListener("input", () => {
      el.input.style.height = "auto";
      el.input.style.height = Math.min(el.input.scrollHeight, 120) + "px";
    });
  }

  init();
})();