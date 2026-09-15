# -*- coding: utf-8 -*-
"""多会话管理：会话 <-> 客服 Agent 实例的映射，含闲置回收。

toC 场景特点：并发用户多、单会话生命周期短、需要按用户隔离上下文。
每个会话持有独立的 Agent（其 AgentState.context 即该用户的对话历史）。
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agentscope.agent import Agent

from .config import Settings


@dataclass
class SessionRuntime:
    """一个运行中的客服会话。"""

    session_id: str
    agent: Agent
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    last_active_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    handed_off: bool = False
    preview: str = ""


class SessionManager:
    """会话注册表（进程内存）。"""

    def __init__(self, settings: Settings, agent_factory: Any) -> None:
        self._settings = settings
        self._agent_factory = agent_factory  # 可延后 import，避免循环引用
        self._sessions: dict[str, SessionRuntime] = {}
        self._gc_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._gc_task = asyncio.create_task(self._gc_loop())

    async def stop(self) -> None:
        if self._gc_task:
            self._gc_task.cancel()
            try:
                await self._gc_task
            except asyncio.CancelledError:
                pass

    async def create(self) -> SessionRuntime:
        # 内存上限淘汰（LRU：按 last_active_at 排序剔除最久未活动的会话）
        if len(self._sessions) >= self._settings.max_sessions:
            stale = sorted(
                self._sessions.values(),
                key=lambda s: s.last_active_at,
            )
            for old in stale[: max(1, len(stale) - self._settings.max_sessions + 1)]:
                self._sessions.pop(old.session_id, None)

        import uuid

        session_id = "sess_" + uuid.uuid4().hex[:12]
        runtime = SessionRuntime(
            session_id=session_id,
            agent=self._agent_factory(session_id),
        )
        self._sessions[session_id] = runtime
        return runtime

    async def get_or_create(self, session_id: str | None) -> SessionRuntime:
        if session_id and session_id in self._sessions:
            runtime = self._sessions[session_id]
            runtime.last_active_at = datetime.now().isoformat(timespec="seconds")
            return runtime
        return await self.create()

    def get(self, session_id: str) -> SessionRuntime | None:
        return self._sessions.get(session_id)

    def list(self) -> list[SessionRuntime]:
        return list(self._sessions.values())

    def remove(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def _update_preview(self, runtime: SessionRuntime) -> None:
        history = runtime.agent.state.context
        for msg in reversed(history):
            if msg.role == "user":
                preview = msg.get_text_content() or ""
                runtime.preview = preview[:32] if preview else ""
                return

    async def _gc_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            ttl = self._settings.session_ttl_minutes * 60
            now = datetime.now().timestamp()
            expired = [
                sid
                for sid, s in self._sessions.items()
                if now - datetime.fromisoformat(s.last_active_at).timestamp() > ttl
            ]
            for sid in expired:
                self._sessions.pop(sid, None)