# -*- coding: utf-8 -*-
"""聊天模型工厂：按配置返回 AgentScope 2.0 的 ChatModelBase 实例。

核心设计：
- create_chat_model: 创建主模型（dashscope / openai / mock）
- create_fallback_model: 创建降级模型（默认 offline mock）
- create_degraded_model: 用 DegradedChatModel 包装主+备，实现自动降级
"""
from agentscope.model import ChatModelBase

from .config import Settings
from .mock_model import OfflineChatModel


def _build_dashscope(settings: Settings) -> ChatModelBase:
    from agentscope.credential import DashScopeCredential
    from agentscope.model import DashScopeChatModel

    assert settings.dashscope_api_key, "缺少 DASHSCOPE_API_KEY"
    return DashScopeChatModel(
        credential=DashScopeCredential(api_key=settings.dashscope_api_key),
        model=settings.model_name,
        stream=True,
    )


def _build_openai(settings: Settings) -> ChatModelBase:
    from agentscope.credential import OpenAICredential
    from agentscope.model import OpenAIChatModel

    assert settings.openai_api_key, "缺少 OPENAI_API_KEY"
    return OpenAIChatModel(
        credential=OpenAICredential(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        ),
        model=settings.model_name,
        stream=True,
    )


def create_chat_model(settings: Settings) -> ChatModelBase:
    """根据配置创建聊天模型（主模型）。"""
    if settings.model_provider == "dashscope":
        return _build_dashscope(settings)
    if settings.model_provider == "openai":
        return _build_openai(settings)
    return OfflineChatModel()


def create_fallback_model(settings: Settings) -> ChatModelBase:
    """创建降级模型（默认 offline mock，零依赖）。"""
    if settings.fallback_provider == "dashscope" and settings.dashscope_api_key:
        return _build_dashscope(
            Settings(
                model_provider="dashscope",
                model_name=settings.fallback_model,
                dashscope_api_key=settings.dashscope_api_key,
                openai_api_key=settings.openai_api_key,
                openai_base_url=settings.openai_base_url,
            )
        )
    if settings.fallback_provider == "openai" and settings.openai_api_key:
        return _build_openai(
            Settings(
                model_provider="openai",
                model_name=settings.fallback_model,
                dashscope_api_key=settings.dashscope_api_key,
                openai_api_key=settings.openai_api_key,
                openai_base_url=settings.openai_base_url,
            )
        )
    return OfflineChatModel()


def create_degraded_model(settings: Settings) -> ChatModelBase:
    """创建带服务降级的模型实例。

    - 若 degradation_enabled=True：返回 DegradedChatModel，主模型失败自动降级
    - 若 degradation_enabled=False：直接返回主模型（与旧行为一致）
    """
    primary = create_chat_model(settings)

    if not settings.degradation_enabled:
        return primary

    from .degradation import DegradedChatModel

    fallback = create_fallback_model(settings)
    return DegradedChatModel(
        primary=primary,
        fallback=fallback,
        timeout_seconds=settings.fallback_timeout_seconds,
    )
