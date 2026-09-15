# -*- coding: utf-8 -*-
"""客服核心服务：把 AgentScope 2.0 的事件流翻译为前端友好的 SSE 事件。"""
import asyncio
import json
from collections import defaultdict
from typing import AsyncGenerator

from agentscope.event import (
    AgentEvent,
    ReplyEndEvent,
    TextBlockDeltaEvent,
    ThinkingBlockDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultTextDeltaEvent,
    HintBlockEvent,
    RequireUserConfirmEvent,
)
from agentscope.message import Msg, UserMsg, TextBlock

from .config import Settings
from .session_manager import SessionManager
from .tracing import TraceRecorder, trace_store

# 单个工具结果推送到前端的最大字符数（避免刷屏）
_MAX_TOOL_RESULT_LENGTH = 1500


def _sse(payload: dict) -> str:
    """把事件字典编码为一条 SSE 消息。"""
    event = payload.get("event", "message")
    data = json.dumps(payload.get("data", {}), ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n"


class ChatStreamer:
    """把一轮对话的 AgentScope 事件流翻译为 SSE 事件序列。"""

    def __init__(self, settings: Settings, session_id: str) -> None:
        self._settings = settings
        self._session_id = session_id
        self._tool_args: dict[str, str] = defaultdict(str)
        self._tool_results: dict[str, str] = defaultdict(str)
        self._tool_names: dict[str, str] = {}

        # ---- 追踪（调试平台数据源）----
        self._recorder: TraceRecorder | None = None
        # 连续 thinking / text 增量块的边界 span（事件驱动开合）
        self._think_span = None
        self._text_span = None
        # tool_call_id -> 工具调用 span（ToolCallStart -> ToolResultEnd）
        self._tool_spans: dict = {}
        # 工具结果执行子 span（ToolCallEnd -> ToolResultEnd）
        self._tool_exec_spans: dict = {}
        self._ttft_ms: float | None = None   # 首个模型 token 到达时间
        self._tool_rounds: int = 0
        self._agent_span = None             # agent.reply_stream 阶段 span

    async def run(
        self,
        agent,
        message: str,
    ) -> AsyncGenerator[str, None]:
        yield _sse({
            "event": "meta",
            "data": {
                "session_id": self._session_id,
                "agent_name": self._settings.agent_name,
                "brand_name": self._settings.brand_name,
                "model_provider": self._settings.model_provider,
                "model_name": self._settings.model_name,
            },
        })

        try:
            self._recorder = trace_store.trace_begin(
                self._session_id, message, self._settings.model_name
            )
            root = self._recorder.begin_span(
                kind="root",
                name="处理用户消息 handle_chat",
                parent=None,
                detail={"message_preview": message[:80]},
            )
            self._agent_span = self._recorder.begin_span(
                kind="agent",
                name="客服智能体 小云 · reply_stream",
                parent=root.id,
                detail={"agent": "小云", "model": self._settings.model_name},
            )
            async for evt in agent.reply_stream(
                UserMsg(name="用户", content=[TextBlock(text=message)]),
            ):
                self._instrument(evt)
                for chunk in self._translate(evt):
                    yield chunk
        except asyncio.CancelledError:
            if self._recorder is not None:
                trace_store.trace_fail(self._recorder.trace_id, "取消")
            raise
        except Exception as exc:  # noqa: BLE001
            if self._recorder is not None:
                trace_store.trace_fail(self._recorder.trace_id, str(exc))
            else:
                trace_store.fail_last(self._session_id, str(exc))
            yield _sse({"event": "error", "data": {"message": str(exc)}})
        finally:
            rec = self._recorder
            if rec is not None:
                if self._agent_span is not None:
                    rec.close_span(
                        self._agent_span.id,
                        attrs={
                            "ttft_ms": self._ttft_ms,
                            "tool_rounds": self._tool_rounds,
                        },
                    )
                    self._agent_span = None
                # 落盘本轮关键信息，供调试平台列表/统计使用
                rec.trace.ttft_ms = self._ttft_ms
                rec.trace.rounds = self._tool_rounds + 1 if self._tool_rounds else 1
                rec.set_tools(self._tools_used())
                if rec.root_id is not None:
                    rec.close_span(
                        rec.root_id,
                        attrs={"total_ms_all_stages": rec.now()},
                    )
                trace_store.trace_finish(rec)
                self._recorder = None

    def _translate(self, evt: AgentEvent) -> list[str]:
        out: list[str] = []

        if isinstance(evt, TextBlockDeltaEvent):
            out.append(_sse({"event": "delta", "data": {"text": evt.delta}}))
        elif isinstance(evt, ThinkingBlockDeltaEvent):
            out.append(_sse({"event": "thinking", "data": {"delta": evt.delta}}))
        elif isinstance(evt, ToolCallStartEvent):
            self._tool_names[evt.tool_call_id] = evt.tool_call_name
            out.append(_sse({
                "event": "tool_call",
                "data": {"id": evt.tool_call_id, "name": evt.tool_call_name},
            }))
        elif isinstance(evt, ToolCallDeltaEvent):
            self._tool_args[evt.tool_call_id] += evt.delta
        elif isinstance(evt, ToolCallEndEvent):
            args = self._tool_args.get(evt.tool_call_id, "")
            out.append(_sse({
                "event": "tool_call_args",
                "data": {"id": evt.tool_call_id, "args": args},
            }))
        elif isinstance(evt, ToolResultTextDeltaEvent):
            self._tool_results[evt.tool_call_id] += evt.delta
        elif isinstance(evt, ToolResultEndEvent):
            result = self._tool_results.get(evt.tool_call_id, "")
            result = result[:_MAX_TOOL_RESULT_LENGTH]
            out.append(_sse({
                "event": "tool_result",
                "data": {
                    "id": evt.tool_call_id,
                    "name": self._tool_names.get(evt.tool_call_id, evt.tool_call_id),
                    "state": evt.state,
                    "result": result,
                },
            }))
        elif isinstance(evt, ReplyEndEvent):
            out.append(_sse({
                "event": "done",
                "data": {"finished_reason": evt.finished_reason},
            }))
        elif isinstance(evt, HintBlockEvent):
            hint = evt.hint
            text = hint if isinstance(hint, str) else " ".join(
                getattr(b, "text", "") for b in hint
            )
            out.append(_sse({"event": "hint", "data": {"text": text}}))
        elif isinstance(evt, RequireUserConfirmEvent):
            out.append(_sse({"event": "confirm_required", "data": {}}))

        return out

    # ---------------- 追踪埋点 ----------------

    def _mark_ttft(self) -> None:
        """首个模型增量（thinking / text）到达的时间，用于衡量首 token 延迟。"""
        if self._recorder is not None and self._ttft_ms is None:
            self._ttft_ms = self._recorder.now()

    def _tools_used(self) -> list:
        return list({v: None for v in self._tool_names.values()}.keys())

    def _close_phase_spans(self) -> None:
        """关闭当前未闭合的 thinking / text 连续块 span。"""
        rec = self._recorder
        if rec is None:
            return
        if self._think_span is not None:
            chars = int(self._think_span.attrs.get("chars", 0))
            rec.close_span(self._think_span.id, attrs={"chars": chars})
            self._think_span = None
        if self._text_span is not None:
            chars = int(self._text_span.attrs.get("chars", 0))
            rec.close_span(self._text_span.id, attrs={"chars": chars})
            self._text_span = None

    def _tool_call_span(self, evt: ToolCallStartEvent) -> None:
        rec = self._recorder
        if rec is None:
            return
        self._mark_ttft()
        self._close_phase_spans()
        span = rec.begin_span(
            kind="tool",
            name=f"工具调用 {evt.tool_call_name}",
            parent=self._agent_span.id if self._agent_span is not None else None,
            detail={"tool_name": evt.tool_call_name},
        )
        self._tool_spans[evt.tool_call_id] = span
        self._tool_rounds += 1

    def _instrument(self, evt: AgentEvent) -> None:
        """把 AgentScope 事件翻成 span 开合动作（不做 SSE 翻译）。"""
        rec = self._recorder
        if rec is None:
            return

        if isinstance(evt, ThinkingBlockDeltaEvent):
            self._mark_ttft()
            if self._text_span is not None:
                self._close_phase_spans()
            if self._think_span is None:
                self._think_span = rec.begin_span(
                    kind="thinking",
                    name="模型思考（思维链）",
                    parent=self._agent_span.id if self._agent_span is not None else None,
                )
            else:
                rec.touch_span(self._think_span.id)
            self._think_span.attrs["chars"] = (
                int(self._think_span.attrs.get("chars", 0)) + len(evt.delta or "")
            )

        elif isinstance(evt, TextBlockDeltaEvent):
            self._mark_ttft()
            if self._think_span is not None:
                self._close_phase_spans()
            if self._text_span is None:
                self._text_span = rec.begin_span(
                    kind="text",
                    name="生成回复文本",
                    parent=self._agent_span.id if self._agent_span is not None else None,
                )
            else:
                rec.touch_span(self._text_span.id)
            self._text_span.attrs["chars"] = (
                int(self._text_span.attrs.get("chars", 0)) + len(evt.delta or "")
            )

        elif isinstance(evt, ToolCallStartEvent):
            self._tool_call_span(evt)

        elif isinstance(evt, ToolCallDeltaEvent):
            span = self._tool_spans.get(evt.tool_call_id)
            if span is not None:
                rec.touch_span(span.id)

        elif isinstance(evt, ToolCallEndEvent):
            span = self._tool_spans.get(evt.tool_call_id)
            if span is not None:
                args = self._tool_args.get(evt.tool_call_id, "")
                span.detail["args"] = args[:600]
                rec.touch_span(span.id)
            parent_tool = self._tool_spans.get(evt.tool_call_id)
            exec_span = rec.begin_span(
                kind="tool_exec",
                name="工具执行+结果回填",
                parent=parent_tool.id if parent_tool is not None else None,
            )
            self._tool_exec_spans[evt.tool_call_id] = exec_span

        elif isinstance(evt, ToolResultTextDeltaEvent):
            span = self._tool_spans.get(evt.tool_call_id)
            if span is not None:
                rec.touch_span(span.id)
            exec_span = self._tool_exec_spans.get(evt.tool_call_id)
            if exec_span is not None:
                rec.touch_span(exec_span.id)

        elif isinstance(evt, ToolResultEndEvent):
            result = self._tool_results.get(evt.tool_call_id, "")
            span = self._tool_spans.pop(evt.tool_call_id, None)
            if span is not None:
                rec.close_span(span.id, attrs={
                    "state": str(evt.state),
                    "result_preview": result[:400],
                })
            exec_span = self._tool_exec_spans.pop(evt.tool_call_id, None)
            if exec_span is not None:
                rec.close_span(exec_span.id, attrs={
                    "state": str(evt.state),
                    "result_preview": result[:400],
                })

        elif isinstance(evt, ReplyEndEvent):
            self._close_phase_spans()
            rec.notify_event(
                "reply_end",
                "推理轮结束 · "
                f"finished_reason={evt.finished_reason}",
            )


async def stream_chat(
    manager: SessionManager,
    settings: Settings,
    session_id: str,
    message: str,
) -> AsyncGenerator[str, None]:
    """流式处理一条用户消息，产出 SSE 文本。"""
    runtime = await manager.get_or_create(session_id)
    # 先手动把用户消息写入上下文（供 preview / 历史使用），
    # 因为 reply_stream 之后仍会由 Agent 写入，这里不再重复写。
    async with runtime.lock:
        streamer = ChatStreamer(settings, runtime.session_id)
        async for chunk in streamer.run(runtime.agent, message):
            yield chunk
    manager._update_preview(runtime)


def build_history(runtime) -> list[dict]:
    """从 AgentState 上下文重建历史消息（供前端回放）。"""
    messages: list[dict] = []
    for msg in runtime.agent.state.context:
        if msg.role == "user":
            messages.append({
                "role": "user",
                "text": msg.get_text_content() or "",
                "thinking": "",
                "tool_calls": [],
            })
        elif msg.role == "assistant":
            tool_calls: list[dict] = []
            results: dict[str, str] = {}
            for blk in msg.get_content_blocks("tool_result"):
                output = blk.output
                if isinstance(output, list):
                    output = "".join(getattr(b, "text", "") for b in output)
                results[blk.id] = output[:_MAX_TOOL_RESULT_LENGTH]
            for blk in msg.get_content_blocks("tool_call"):
                tool_calls.append({
                    "id": blk.id,
                    "name": blk.name,
                    "args": blk.input,
                    "result": results.get(blk.id, ""),
                })
            messages.append({
                "role": "assistant",
                "text": "\n".join(
                    b.text for b in msg.get_content_blocks("text") if b.text
                ),
                "thinking": "\n".join(
                    b.thinking for b in msg.get_content_blocks("thinking")
                ),
                "tool_calls": tool_calls,
            })
    return messages
