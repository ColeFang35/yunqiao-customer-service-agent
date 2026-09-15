# -*- coding: utf-8 -*-
"""轻量推理链路追踪：为调试平台提供火焰图 / 调用树数据。

每处理一条用户消息生成一条 Trace，内部由若干 Span 组成（嵌套、带耗时）：

- 调用树：单条 trace 的 span 按 parent 关系组织成树
- 火焰图：跨多条 trace 或单条 trace，把 span 按 (kind, name) 路径聚合耗时

纯内存、无第三方依赖，容量有界（默认保留最近 200 条）。
"""
import threading
import time
import timeit
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime


@dataclass
class SpanRecord:
    """一个推理阶段的测量点（a span）。"""

    id: str
    trace_id: str
    name: str
    kind: str                 # root | agent | think | text | tool | tool_exec | tool_args | event
    parent: str | None = None
    start_ms: float = 0.0     # 相对 trace 起始
    end_ms: float = 0.0
    round_index: int = 0      # 第几轮 ReAct 循环（从 1 起，0 表示贯穿全轮）
    detail: dict = field(default_factory=dict)
    status: str = "ok"        # ok | error
    open: bool = True

    @property
    def attrs(self) -> dict:
        """service 埋点里以 span.attrs 访问动态属性，统一指向 detail。"""
        return self.detail

    @property
    def duration_ms(self) -> float:
        end = self.end_ms if self.end_ms > 0 else self.start_ms
        return max(0.0, end - self.start_ms)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration_ms"] = round(self.duration_ms, 1)
        d["start_ms"] = round(self.start_ms, 1)
        d["end_ms"] = round(self.end_ms, 1) if self.end_ms > 0 else None
        return d


@dataclass
class TraceRecord:
    """一条用户消息的完整推理记录。"""

    id: str
    session_id: str
    message: str
    started_at: str
    model_name: str = ""
    brand_name: str = "云雀商城"
    status: str = "running"   # running | done | failed
    error: str | None = None
    spans: list = field(default_factory=list)   # [SpanRecord]
    events: list = field(default_factory=list)  # [{id,name,kind,at_ms,note}]
    total_ms: float = 0.0
    rounds: int = 1
    tool_calls_num: int = 0
    tools: list = field(default_factory=list)
    ttft_ms: float | None = None
    tokens_chars: int = 0     # 用字符数近似（离线 mock 场景下）

    def summary(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "message": self.message[:80],
            "started_at": self.started_at,
            "model_name": self.model_name,
            "brand_name": self.brand_name,
            "status": self.status,
            "error": self.error,
            "total_ms": round(self.total_ms, 1),
            "rounds": self.rounds,
            "tool_calls_num": self.tool_calls_num,
            "tools": list(self.tools),
            "ttft_ms": round(self.ttft_ms, 1) if self.ttft_ms is not None else None,
            "spans_num": len(self.spans),
        }

    def detail(self) -> dict:
        d = self.summary()
        d["message"] = self.message
        d["spans"] = [s.to_dict() for s in self.spans]
        d["events"] = list(self.events)
        d["tokens_chars"] = self.tokens_chars
        return d


class TraceRecorder:
    """一个 trace 的记录器，供流式过程中埋点调用。"""

    def __init__(self, store: "TraceStore", session_id: str, message: str,
                 model_name: str, brand_name: str) -> None:
        self.store = store
        self.trace = TraceRecord(
            id="trc_" + uuid.uuid4().hex[:12],
            session_id=session_id,
            message=message,
            started_at=datetime.now().isoformat(timespec="seconds"),
            model_name=model_name,
            brand_name=brand_name,
        )
        self._t0 = timeit.default_timer()
        self._open_stack: list[SpanRecord] = []   # 当前未闭合的 span 栈
        self.root_id: str | None = None
        self._committed: bool = False

    # ---- 原生属性供 service 快捷读取 ----
    @property
    def trace_id(self) -> str:
        return self.trace.id

    def now(self) -> float:
        """当前相对 trace 起始的毫秒数。"""
        return (timeit.default_timer() - self._t0) * 1000.0

    # ---- Span 生命周期 ----
    def begin_span(self, kind: str, name: str, *, parent: str | None = None,
                   detail: dict | None = None,
                   round_index: int = 0) -> SpanRecord:
        span = SpanRecord(
            id="sp_" + uuid.uuid4().hex[:8],
            trace_id=self.trace.id,
            name=name,
            kind=kind,
            parent=parent,
            start_ms=self.now(),
            detail=detail or {},
            round_index=round_index,
        )
        self.trace.spans.append(span)
        self._open_stack.append(span)
        if self.root_id is None:
            self.root_id = span.id
        return span

    def touch_span(self, span_id: str) -> None:
        """滚动刷新 span 的结束时间（用于 thinking/text 连续块）。"""
        for sp in self._open_stack:
            if sp.id == span_id:
                sp.end_ms = self.now()
                return

    def close_span(self, span_id: str, *, attrs: dict | None = None) -> None:
        remain: list[SpanRecord] = []
        for sp in self._open_stack:
            if sp.id == span_id:
                sp.end_ms = self.now()
                sp.open = False
                if attrs:
                    sp.detail.update(attrs)
            else:
                remain.append(sp)
        self._open_stack = remain

    def notify_event(self, name: str, note: str, *, kind: str = "event",
                     at_ms: float | None = None) -> None:
        """记录瞬时事件（时间点 marker，非 span），用于推理步骤时间轴。"""
        self.trace.events.append({
            "id": "ev_" + uuid.uuid4().hex[:8],
            "name": name,
            "kind": kind,
            "at_ms": round(at_ms if at_ms is not None else self.now(), 1),
            "note": note,
        })

    # ---- 快捷埋点信息 ----
    def set_ttft(self) -> None:
        if self.trace.ttft_ms is None:
            self.trace.ttft_ms = self.now()

    def set_tools(self, names: list[str]) -> None:
        merged = list(self.trace.tools)
        for n in names:
            if n not in merged:
                merged.append(n)
        self.trace.tools = merged
        self.trace.tool_calls_num = len(merged)

    def add_chars(self, n: int) -> None:
        self.trace.tokens_chars += max(0, n)

    # ---- 结束 ----
    def finish(self, finished_reason: str = "completed") -> None:
        for sp in self._open_stack:
            sp.end_ms = self.now()
            sp.open = False
        self._open_stack = []
        self.trace.total_ms = round(self.now(), 1)
        self.trace.status = "done"

    def fail(self, error: str) -> None:
        for sp in self._open_stack:
            sp.end_ms = self.now()
            sp.open = False
            sp.status = "error"
        self._open_stack = []
        self.trace.total_ms = round(self.now(), 1)
        self.trace.status = "failed"
        self.trace.error = error


class TraceStore:
    """保存最近 N 条已落盘（或进行中）trace 的环形仓库。"""

    def __init__(self, capacity: int = 200) -> None:
        self._traces: deque[TraceRecord] = deque(maxlen=capacity)
        self._by_id: dict[str, TraceRecord] = {}
        self._inflight: dict[str, TraceRecorder] = {}
        self._lock = threading.RLock()

    # ---- 生命周期 ----
    def trace_begin(self, session_id: str, message: str,
                    model_name: str = "", brand_name: str = "云雀商城") -> TraceRecorder:
        recorder = TraceRecorder(self, session_id, message, model_name, brand_name)
        with self._lock:
            self._inflight[recorder.trace_id] = recorder
        return recorder

    def _commit(self, recorder: TraceRecorder) -> None:
        if recorder._committed:
            return
        recorder._committed = True
        if recorder.trace.status == "running":
            recorder.finish()
        with self._lock:
            rec = recorder.trace
            self._traces.append(rec)
            self._by_id[rec.id] = rec
            self._inflight.pop(rec.id, None)
            # 与 deque 容量匹配的索引清理
            ids = {t.id for t in self._traces}
            for tid in [k for k in self._by_id if k not in ids]:
                del self._by_id[tid]

    def trace_finish(self, recorder: TraceRecorder) -> None:
        self._commit(recorder)

    def trace_fail(self, trace_id: str, error: str) -> bool:
        with self._lock:
            recorder = self._inflight.get(trace_id)
        if recorder is None:
            return False
        recorder.fail(error)
        self._commit(recorder)
        return True

    def fail_last(self, session_id: str, error: str) -> bool:
        """message 流不起时兜底：把与该 session 最近的进行中 trace 标记失败。"""
        with self._lock:
            candidates = [r for r in self._inflight.values()
                          if r.trace.session_id == session_id]
        if not candidates:
            return False
        recorder = candidates[-1]
        recorder.fail(error)
        self._commit(recorder)
        return True

    # ---- 查询 ----
    def latest_trace(self) -> TraceRecord | None:
        """最近一条已结束的 trace；没有则给正在进行的。"""
        if self._traces:
            return self._traces[-1]
        if self._inflight:
            return list(self._inflight.values())[-1].trace
        return None

    def list_recent(self, limit: int = 50, search: str | None = None,
                    session_id: str | None = None) -> list[TraceRecord]:
        items = list(self._traces)[-limit:]
        if session_id:
            items = [t for t in items if t.session_id == session_id]
        if search:
            kw = search.lower()
            items = [t for t in items
                     if kw in t.message.lower()
                     or kw in t.id.lower()
                     or any(kw in w for w in t.tools)
                     or (t.model_name and kw in t.model_name.lower())]
        return list(reversed(items))

    def get(self, trace_id: str) -> TraceRecord | None:
        rec = self._by_id.get(trace_id)
        if rec is not None:
            return rec
        with self._lock:
            rec_inflight = self._inflight.get(trace_id)
        return rec_inflight.trace if rec_inflight else None

    def clear(self) -> int:
        with self._lock:
            n = len(self._traces) + len(self._inflight)
            self._traces.clear()
            self._by_id.clear()
            self._inflight.clear()
        return n

    # ---- 统计 ----
    def stats(self) -> dict:
        items = list(self._traces)
        total = len(items)
        ok = sum(1 for t in items if t.status == "done")
        failed = sum(1 for t in items if t.status == "failed")
        tool_stats: dict[str, dict] = {}
        total_ms_all = 0.0
        total_rounds = 0
        total_ttft: list[float] = []
        for t in items:
            total_ms_all += t.total_ms
            total_rounds += t.rounds
            if t.ttft_ms is not None:
                total_ttft.append(t.ttft_ms)
            for sp in t.spans:
                if sp.kind == "tool":
                    name = sp.detail.get("tool_name", sp.name)
                    tt = tool_stats.setdefault(
                        name, {"count": 0, "total_ms": 0.0, "max_ms": 0.0, "errors": 0})
                    tt["count"] += 1
                    dur = sp.duration_ms
                    tt["total_ms"] += dur
                    tt["max_ms"] = max(tt["max_ms"], dur)
                    if sp.status != "ok":
                        tt["errors"] += 1
        avg_ms = total_ms_all / total if total else 0.0
        avg_ttft = sum(total_ttft) / len(total_ttft) if total_ttft else 0.0
        return {
            "total": total,
            "ok": ok,
            "failed": failed,
            "inflight": len(self._inflight),
            "avg_total_ms": round(avg_ms, 1),
            "avg_ttft_ms": round(avg_ttft, 1),
            "rounds_total": total_rounds,
            "tools": tool_stats,
        }

    # ---- 火焰图聚合 ----
    def _merge_into_flame(self, root: dict, span: SpanRecord,
                          trace_total_ms: float) -> None:
        """把一条 span 按 (kind,name) 路径合并进聚合火焰树。"""
        node = self._find_or_add(root, span.kind, span.name)
        node["value"] += span.duration_ms if span.duration_ms > 0 else 0.1
        node["count"] += 1
        node["trace_total_ms"] = max(node.get("trace_total_ms", 0.0), trace_total_ms)
        for child_span in [s for s in root["src_trace"].spans if s.parent == span.id]:
            node["src_trace"] = root["src_trace"]
            self._merge_children(node, child_span, root)

    def _merge_children(self, parent_node: dict, span: SpanRecord, root: dict) -> None:
        node = self._find_or_add(parent_node, span.kind, span.name)
        node["value"] += max(span.duration_ms, 0.1)
        node["count"] += 1
        for child_span in [s for s in root["src_trace"].spans if s.parent == span.id]:
            self._merge_children(node, child_span, root)

    @staticmethod
    def _find_or_add(parent: dict, kind: str, name: str) -> dict:
        for child in parent["children"]:
            if child["kind"] == kind and child["name"] == name:
                return child
        node = {"kind": kind, "name": name, "value": 0.0,
                "count": 0, "children": []}
        parent["children"].append(node)
        return node

    def span_tree(self, trace: TraceRecord) -> dict:
        """单条 trace 的 span 树（供调用树 / 单条火焰图）。"""
        nodes: dict[str, dict] = {}
        for sp in trace.spans:
            nodes[sp.id] = {
                "id": sp.id,
                "kind": sp.kind,
                "name": sp.name,
                "value": max(sp.duration_ms, 0.1),
                "count": 1,
                "round_index": sp.round_index,
                "status": sp.status,
                "detail": sp.detail,
                "children": [],
                "parent": sp.parent,
            }
        root = {"id": "__root__", "kind": "root",
                "name": "处理消息 chat", "value": max(trace.total_ms, 0.1),
                "count": 1, "children": [], "parent": None, "detail": {
                    "message": trace.message,
                    "model": trace.model_name,
                    "status": trace.status,
                    "error": trace.error,
                }}
        for sp in trace.spans:
            node = nodes[sp.id]
            if sp.parent and sp.parent in nodes:
                nodes[sp.parent]["children"].append(node)
            else:
                root["children"].append(node)
        # 子 span 按开始时间排序
        def sort_children(n: dict) -> None:
            n["children"].sort(key=lambda c: c.get("start_ms", 0))
            for c in n["children"]:
                sort_children(c)
        # 给 node 补 start_ms 供前端瀑布定位
        for sp in trace.spans:
            nodes[sp.id]["start_ms"] = round(sp.start_ms, 1)
        sort_children(root)
        return root

    def flamegraph(self, trace_id: str | None = None,
                   limit: int = 100) -> dict:
        """返回火焰图树。
        - 指定 trace_id：该条 trace 的 span 树（权重=耗时）。
        - 否则：最近 N 条 trace 的聚合火焰图（按 kind+name 路径聚合耗时）。
        """
        if trace_id:
            rec = self.get(trace_id)
            if rec is None:
                return {"name": "空", "kind": "root", "value": 0.0,
                        "count": 0, "children": []}
            return self.span_tree(rec)

        items = list(self._traces)[-limit:]
        root = {"kind": "root", "name": "全部推理请求", "value": 0.0,
                "count": 0, "children": []}
        for rec in items:
            # 每条 trace 贡献一棵树（按其 span 父子关系聚合）
            tree = self.span_tree(rec)
            for child in tree["children"]:
                self._flame_merge(root, child)
            root["value"] += max(rec.total_ms, 0.1)
        root["count"] = len(items)
        return root

    def _flame_merge(self, parent: dict, node: dict) -> None:
        for child in parent["children"]:
            if child["kind"] == node["kind"] and child["name"] == node["name"]:
                child["value"] += node["value"]
                child["count"] += node.get("count", 1)
                for g in node["children"]:
                    self._flame_merge(child, g)
                return
        parent["children"].append({
            "kind": node["kind"],
            "name": node["name"],
            "value": node["value"],
            "count": node.get("count", 1),
            "children": [self._copy_flame(c) for c in node["children"]],
        })

    def _copy_flame(self, node: dict) -> dict:
        return {
            "kind": node["kind"],
            "name": node["name"],
            "value": node["value"],
            "count": node.get("count", 1),
            "children": [self._copy_flame(c) for c in node["children"]],
        }


# ---- 全局单例 ----
trace_store = TraceStore(capacity=200)
