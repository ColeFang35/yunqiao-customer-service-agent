# -*- coding: utf-8 -*-
"""基于 AgentScope 2.0 SDK 层构建客服智能体。

编排层次对应 AgentScope 2.0 架构：
- Building blocks: Agent（ReAct 式推理-行动循环）
- Model: DashScopeChatModel / OpenAIChatModel / OfflineChatModel
- Toolkit: FunctionTool 注册业务工具
- State: AgentState 维护会话上下文 + 权限上下文（服务端全托管，BYPASS 模式）
"""
from agentscope.agent import Agent
from agentscope.agent._config import InjectionConfig, ModelConfig
from agentscope.message import Msg
from agentscope.model import ChatModelBase
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.state import AgentState

from .config import Settings
from .models import create_degraded_model
from .prompts import build_system_prompt
from .tools import build_toolkit


def build_customer_service_agent(
    settings: Settings,
    session_id: str,
    history: list[Msg] | None = None,
    model: ChatModelBase | None = None,
) -> Agent:
    """为单个会话创建一个客服 Agent 实例。

    Args:
        settings: 全局配置。
        session_id: 会话 ID（会写入 AgentState，便于事件追踪与持久化）。
        history: 恢复历史对话时传入的上下文消息。
        model: 可选的自定义模型实例（测试注入用）。
    """
    state = AgentState(
        session_id=session_id,
        context=history or [],
        # 服务端全托管场景：工具属平台内部可信能力，走 BYPASS 放行，
        # 避免每次工具调用都要求 C 端用户逐条确认。
        permission_context=PermissionContext(mode=PermissionMode.BYPASS),
    )
    return Agent(
        name=settings.agent_name,
        system_prompt=build_system_prompt(settings.agent_name, settings.brand_name),
        model=model or create_degraded_model(settings),
        toolkit=build_toolkit(),
        state=state,
        model_config=ModelConfig(max_retries=2),
        injection_config=InjectionConfig(timezone="Asia/Shanghai"),
    )