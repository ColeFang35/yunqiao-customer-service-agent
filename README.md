# 云雀商城 toC 智能客服系统

基于 [AgentScope 2.0](https://github.com/agentscope-ai/agentscope) 构建的电商智能客服 Agent。用 ReAct 循环驱动推理与工具调用，覆盖「浏览商品 → 加购物车 → 下单 → 支付 → 查订单/物流 → 退款 → 转人工」完整链路；前端通过 SSE 实时展示思考过程、工具调用与回复。

**无需 API Key**：内置离线规则模型，clone 下来即可完整跑通（含真实工具调用与多轮对话）。

## 亮点

- **17 个业务工具**覆盖售前/售中/售后全流程；查询类工具只读，写操作（退款 / 取消 / 建工单）先向用户说明影响、确认后执行。
- **业务数据不编造**：涉及订单、物流、退款、用户信息时必须调用工具拿真实数据，模型没有"直接回答"的捷径。
- **可视化调试平台（Fire Trace）**：每轮推理记录为 span 树，前端提供调用树、火焰图、瀑布时间线，看清首 token 延迟与每步工具耗时。
- **三档模型 + 自动降级**：通义千问 / 任意 OpenAI 兼容端点 / 离线规则模型可切换；主模型超时或异常时自动切备用模型。
- **多会话与多轮上下文**：会话级 Agent + LRU 淘汰与闲置回收；支持指代消解与实体继承，能理解"帮我退了""那物流呢"这类追问。

## 快速开始

需要 Python 3.10+。

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py                    # 默认 http://127.0.0.1:8000
```

打开 `http://127.0.0.1:8000` 即用，健康检查 `GET /api/health`。默认走离线模型，不需要任何 Key。

要用真实模型：复制 `.env.example` 为 `.env`，填 `DASHSCOPE_API_KEY`（或 `OPENAI_API_KEY` + `OPENAI_BASE_URL`）。

## 试试这些（内置演示数据）

| 说什么 | 会发生什么 |
|---|---|
| 想买个降噪耳机，推荐一下 | 调用商品查询工具，返回真实商品 |
| 把 P1001 加进购物车 2 个 | 加入购物车 |
| 把购物车里的东西下单，送到文三路 199 号 | 生成订单 |
| 帮我查一下订单 SO20260810001 的物流 | 返回物流轨迹 |
| 我想把 SO20260812003 这个订单退了 | 查退款资格 → 说明影响 → 提交申请 |
| 手机号后四位 3721，看下我最近的订单 | 按手机号定位订单 |
| 我要转人工客服 | 创建工单并转人工 |

演示订单：`SO20260810001`（已发货）、`SO20260812003`（待发货，可退）、`SO20260815005`（待付款，可取消）。

## 架构

每个会话持有一个 AgentScope `Agent`。一条用户消息走一轮 ReAct：模型推理（thinking）→ 决定工具调用（tool_call）→ 执行工具（tool_exec）→ 结果回填上下文 → 生成回复（delta）。全过程以标准事件流对外广播，`ChatStreamer` 翻译为 SSE。

| AgentScope 概念 | 本项目实现 |
|---|---|
| `Agent` | `backend/agent_factory.py` — 装配模型 / 工具集 / 状态 / 系统提示词 |
| `ChatModelBase` | `backend/models.py`、`mock_model.py` — 三档模型统一实现 |
| `FunctionTool` / `Toolkit` | `backend/tools/` — 17 个工具，从 docstring 自动解析 schema |
| `AgentState` | `backend/session_manager.py` — 每会话独立上下文 |
| 事件流 | `backend/service.py` — 订阅事件并翻译为 SSE |
| 追踪 | `backend/tracing.py` — Span / Trace，供调试平台使用 |

## 管理后台

`http://127.0.0.1:8000/admin`：会话监控、工单处理、FAQ 与商品增删改查（改动即时生效）、订单与用户浏览。

## 调试平台（Fire Trace）

`http://127.0.0.1:8000/debug`：推理总览（成功率 / 平均耗时 / 平均 TTFT）、Trace 列表、调用树、火焰图（单条与聚合）、瀑布时间线。数据在内存中，随进程存在，无需数据库。

接口前缀 `/api/debug`：`summary`、`traces`、`traces/{id}`、`flamegraph`、`latest`、`clear`。

## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 健康检查（当前 provider / model） |
| `POST` | `/api/sessions` | 创建会话 |
| `GET` | `/api/sessions/{id}/history` | 回放会话历史（含思考与工具调用） |
| `POST` | `/api/chat` | 发送消息，返回 SSE 事件流 |

`POST /api/chat` 请求体：

```json
{ "session_id": "sess_xxxx", "message": "帮我查订单 SO20260810001 的物流" }
```

SSE 事件：`meta` / `thinking` / `delta` / `tool_call` / `tool_call_args` / `tool_result` / `done` / `error` / `hint`。

## 项目结构

```
backend/
  agent_factory.py    Agent / Toolkit / State 装配
  models.py           模型工厂（dashscope / openai / mock）
  mock_model.py       离线规则模型
  service.py          事件流 → SSE 翻译
  tracing.py          推理链路追踪（Span / Trace / 火焰图）
  debug.py            调试平台 API
  admin.py            管理后台 API
  server.py           FastAPI：REST + SSE + 静态前端
  session_manager.py  多会话管理（LRU + 闲置回收）
  tools/              17 个业务工具
  store/              JSON 数据仓库
  data/               演示数据
web/                  聊天页 / 管理后台 / 调试平台
tests/test_flow.py    端到端 + SSE + 调试接口冒烟测试
run.py                启动入口
```

## 测试

```bash
pytest -v
```

使用离线模型，无需网络与 Key。覆盖物流查询、退款链路、FAQ 检索、转人工、购物下单支付、多轮追问（指代继承、短句确认）、多条件选品，以及 SSE 与调试接口冒烟。测试会自动备份并还原 `backend/data/`，不污染演示数据。

## 接入真实系统

业务数据统一由 `backend/store/mock_store.py` 暴露（订单 / 用户 / FAQ / 工单 / 商品 / 购物车，JSON + 文件锁实现）。接入真实中台时保持 Store 方法签名不变、替换内部实现即可，工具层与 Agent 层无需改动。FAQ 检索（`backend/tools/knowledge.py`）可替换为 Qdrant / Milvus + Embedding。

## 安全

只读查询与写操作工具分离；手机号等敏感信息只展示后四位；生产部署建议为 `/api/chat` 增加鉴权与限流。
