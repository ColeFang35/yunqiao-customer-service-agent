# -*- coding: utf-8 -*-
"""HTTP 接口的请求 / 响应模型。"""
from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """发起一次对话请求。"""

    session_id: str | None = Field(
        default=None,
        description="会话 ID，缺省时自动创建新会话",
    )
    message: str = Field(min_length=1, max_length=4000, description="用户消息")


class SessionInfo(BaseModel):
    """会话概要信息。"""

    session_id: str
    created_at: str
    last_active_at: str
    message_count: int
    preview: str = ""
    handed_off: bool = False


class HistoryMessage(BaseModel):
    """用于前端回放历史的一条消息。"""

    role: Literal["user", "assistant"]
    text: str = ""
    thinking: str = ""
    tool_calls: list[dict] = Field(default_factory=list)


class ConfigResponse(BaseModel):
    """前端初始化所需的配置。"""

    agent_name: str
    brand_name: str
    model_provider: str
    model_name: str
    quick_prompts: list[str]