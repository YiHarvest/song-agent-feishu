# Song Agent 3.0 架构文档

## 项目定位

**Song Agent 3.0** 是一个基于 ReAct 模式的多用户飞书个人智能管家。用户通过自然语言与机器人交互，系统在有限步数内完成意图识别、工具调用和结果返回，帮助用户管理日程、文档和每日计划。

**核心价值**：
- 🗣️ **自然语言交互**：用户无需记忆命令，直接说"帮我整理今天的计划"
- 🔐 **安全可控**：所有写操作需要用户确认，OAuth token 加密存储
- 🔄 **多用户隔离**：每个用户的数据和会话完全隔离
- ⚡ **快速响应**：简单请求直接返回，复杂请求走完整 ReAct 流程

---

## 核心架构

### 1. ReAct Runtime（决策引擎）

基于 ReAct（Reasoning + Acting）模式的智能体运行时：

```
用户输入 → 意图识别 → 工具选择 → 执行 → 观察 → 循环/终止
```

**关键组件**：
- `song_agent/agent/runtime.py`：ReAct 主循环，最多 10 步
- `song_agent/agent/context.py`：上下文管理，包含用户身份、消息、元数据
- `song_agent/agent/models.py`：AgentResult、ToolResult 等数据模型

**优化措施**：
- 系统提示词强调优先使用 `final_answer`，避免不必要的工具调用
- 简单请求（问候、状态查询）直接返回，不走完整流程
- 每步记录决策摘要，用于审计和调试

### 2. Tool Registry（工具注册）

显式注册 LLM 可见的工具，控制权限和可见性：

```python
# song_agent/agent/tool_registry.py
registry.register(AgentTool(
    name="plans.save_draft",
    description="根据用户原始消息整理并保存今天的本地计划草稿。",
    handler=self._tool_plan_draft,
    category="local",  # local/prepare/commit
))
```

**工具分类**：
- `local`：本地操作，无需确认（如保存计划草稿）
- `prepare`：准备操作，需要用户确认（如创建日历事件）
- `commit`：内部执行方法，LLM 不可见

**当前工具**：
- `plans.save_draft`：保存计划草稿
- `calendar.prepare_create_event`：准备日历事件
- `reviews.save`：保存复盘
- `documents.prepare_create`：准备创建文档
- `documents.prepare_append`：准备追加文档

### 3. Policy Guard（权限控制）

在工具执行前检查权限和约束：

```python
# song_agent/policies/tool_policy.py
async def check_permission(context: AgentContext, tool: str) -> bool:
    # 检查用户是否授权
    # 检查工具是否允许
    # 检查参数是否合法
```

**安全边界**：
- LLM 只能看到 `local` 和 `prepare` 工具
- `commit_*` 方法只能由确认后的 Executor 调用
- 卡片回调验证签名，防止伪造

### 4. OAuth 3.0（用户授权）

完整的 OAuth 3.0 授权码流程，支持用户身份代理：

```
用户请求 → 检查授权 → 未授权 → 生成授权URL → 用户授权 → 回调 → 保存token → 继续处理
```

**关键特性**：
- Access token 和 refresh token 使用 AES-256-GCM 加密存储
- Token 按用户隔离，支持多租户
- 授权后自动继续处理原始请求（无需用户重新发送）

**实现文件**：
- `song_agent/feishu/oauth.py`：OAuth 流程管理
- `song_agent/services/encryption.py`：Token 加密服务
- `song_agent/store.py`：Token 持久化

### 5. SQLite + WAL（持久化）

使用 SQLite 作为主数据库，启用 WAL 模式提升并发性能：

**数据表**：
- `users`：用户身份信息
- `oauth_tokens`：加密的 OAuth token
- `oauth_authorizations`：授权状态（包含原始请求）
- `daily_plans`：每日计划记录
- `pending_actions`：待确认的操作
- `outbox`：消息发送队列
- `scheduled_jobs`：定时任务
- `scheduler_leases`：调度器选主

**迁移管理**：
- `song_agent/db/migrations.py`：版本化迁移脚本
- 当前版本：0008（添加 `original_request` 字段）

### 6. APScheduler（定时任务）

基于 APScheduler 的持久化调度器，支持多实例选主：

```python
# song_agent/scheduler/runtime.py
async def start_scheduler():
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    # 每天早上 8:00 提示用户制定计划
    # 每天晚上 20:00 提示用户复盘
```

**关键特性**：
- 使用 SQLite leader lease 实现多实例选主
- Fencing token 防止脑裂
- 任务状态持久化，支持重试

---

## 核心功能

### 1. 日程管理

**创建日程**：
```
用户：明天上午10点提醒我开会
系统：创建日历事件，发送确认卡片
用户：点击确认
系统：写入飞书日历，返回成功消息
```

**重复任务**：
- 支持 `daily`（每天）、`weekdays`（工作日）、`weekly`（每周）
- 用户说："每天早上9点提醒我喝水"
- 系统创建重复日历事件，飞书日历每天推送提醒

**实现**：
- `song_agent/feishu/openapi.py`：调用飞书日历 API
- `song_agent/feishu/mcp.py`：通过飞书官方 MCP 调用

### 2. 文档管理

**创建文档**：
```
用户：创建一份项目进展文档
系统：生成文档草稿，发送确认卡片
用户：点击确认
系统：创建飞书云文档，返回链接
```

**追加文档**：
```
用户：把这段内容追加到项目方案
系统：搜索目标文档，准备追加内容，发送确认卡片
用户：点击确认
系统：追加内容到文档，返回成功消息
```

**实现**：
- `song_agent/feishu/openapi.py`：调用飞书文档 API
- `song_agent/feishu/mcp.py`：通过飞书官方 MCP 调用

### 3. 每日计划

**制定计划**：
```
用户：帮我整理今天的计划：上午写代码，下午开会，晚上看书
系统：解析任务，保存草稿，返回格式化的计划列表
```

**复盘任务**：
```
用户：复盘今天的任务：代码写完了，会议取消了，书看了一半
系统：更新任务状态，计算完成率，生成复盘摘要
```

**实现**：
- `song_agent/planner.py`：解析用户输入，生成结构化计划
- `song_agent/workflow.py`：处理计划和复盘的完整流程

### 4. 快速响应

**简单请求直接返回**：
- 问候："你好" → 直接返回欢迎消息
- 状态查询："今天有什么任务" → 直接返回任务列表
- 感谢："谢谢" → 直接返回礼貌回复

**实现**：
- `song_agent/workflow.py` 中的 `_try_quick_response` 方法
- 避免简单请求走完整 ReAct 流程，提升响应速度

---

## 目录结构

```
song_agent/
├── __init__.py                 # 包初始化
├── __main__.py                 # CLI 入口
├── app.py                      # FastAPI 应用入口，处理 OAuth 回调和卡片回调
├── config.py                   # 配置管理，环境变量加载
├── llm.py                      # LLM 调用封装，支持 OpenAI-compatible API
├── models.py                   # 数据模型定义（消息、任务、记录、令牌等）
├── planner.py                  # 计划解析模块，使用 LLM 解析用户输入
├── store.py                    # SQLite 持久化层，所有数据表操作
├── workflow.py                 # 核心工作流处理器，协调各个模块
│
├── agent/                      # ReAct 智能体核心
│   ├── __init__.py
│   ├── context.py              # Agent 上下文，包含用户身份、消息、元数据
│   ├── models.py               # Agent 数据模型（AgentResult、ToolResult）
│   ├── runtime.py              # ReAct 运行时，决策循环
│   └── tool_registry.py        # 工具注册表，控制 LLM 可见的工具
│
├── cli/                        # 命令行工具
│   ├── __init__.py
│   └── rotate_keys.py          # Token 加密密钥轮换工具
│
├── db/                         # 数据库管理
│   ├── __init__.py
│   └── migrations.py           # 版本化迁移脚本
│
├── feishu/                     # 飞书集成
│   ├── __init__.py
│   ├── cards.py                # 飞书卡片生成（确认卡片）
│   ├── mcp.py                  # 飞书官方 MCP 调用（日历、文档）
│   ├── oauth.py                # OAuth 3.0 授权流程
│   ├── openapi.py              # 飞书 OpenAPI 调用（日历、文档）
│   └── transport.py            # 飞书消息传输（发送消息、接收事件）
│
├── observability/              # 可观测性
│   ├── __init__.py
│   ├── context.py              # 请求上下文和追踪
│   └── redaction.py            # 敏感信息脱敏（token、密钥）
│
├── policies/                   # 权限控制
│   ├── __init__.py
│   └── tool_policy.py          # 工具执行前的权限检查
│
├── scheduler/                  # 定时任务
│   ├── __init__.py
│   ├── lease.py                # SQLite leader lease，多实例选主
│   └── runtime.py              # APScheduler 调度器，持久化任务
│
└── services/                   # 业务服务
    ├── __init__.py
    ├── agent_runs.py           # Agent 运行记录
    ├── audit.py                # 审计日志
    ├── encryption.py           # Token 加密服务（AES-256-GCM）
    ├── outbox.py               # 消息发送队列，支持重试
    ├── pending_actions.py      # 待确认操作管理
    └── reconciliation.py       # 数据一致性检查和修复

tests/                          # 测试套件
├── test_agent_runtime.py       # ReAct 运行时测试
├── test_audit.py               # 审计日志测试
├── test_encryption.py          # Token 加密测试
├── test_identity.py            # 用户身份测试
├── test_llm.py                 # LLM 调用测试
├── test_oauth.py               # OAuth 流程测试
├── test_openapi.py             # 飞书 OpenAPI 测试
├── test_outbox.py              # 消息队列测试
├── test_pending_actions.py     # 待确认操作测试
├── test_planner.py             # 计划解析测试
├── test_scheduler_lease.py     # 调度器选主测试
├── test_store.py               # 持久化层测试
└── test_transport.py           # 消息传输测试
```

---

## 已完成的架构优化

### 建议 1：修复卡片回调错误 ✅

**问题**：卡片回调时，如果缺少签名参数（timestamp、nonce），会导致 500 错误。

**解决方案**：
- 在 `song_agent/app.py` 中添加防御性检查
- 确保必要的签名参数存在后再处理
- 添加异常处理，避免 500 错误

**代码位置**：`song_agent/app.py` 的 `handle_card_action` 函数

### 建议 2：实现快速路径 ✅

**问题**：简单请求（问候、状态查询）也要走完整 ReAct 流程，响应慢。

**解决方案**：
- 在 `song_agent/workflow.py` 中添加 `_try_quick_response` 方法
- 对简单请求直接返回，不走完整流程
- 显著提升响应速度

**代码位置**：`song_agent/workflow.py` 的 `_try_quick_response` 方法

**支持的快速响应**：
- 问候："你好"、"hello"、"hi"
- 状态查询："今天有什么任务"
- 感谢："谢谢"、"感谢"
- 告别："再见"、"晚安"

### 建议 3：优化 LLM 提示词 ✅

**问题**：LLM 容易进行不必要的工具调用，导致响应慢、成本高。

**解决方案**：
- 在 `song_agent/agent/runtime.py` 中优化系统提示词
- 强调优先使用 `final_answer`，避免不必要的工具调用
- 减少无效的 LLM 轮次

**代码位置**：`song_agent/agent/runtime.py` 的 `_system_prompt` 属性

**优化原则**：
1. 优先使用 `final_answer` 直接回答
2. 只在必要时调用工具
3. 避免重复调用同一工具
4. 保持响应简洁

---

## 未来增强方向

### 1. 任务增量修改

**现状**：用户无法修改已创建的任务，只能重新描述完整计划。

**建议**：
- 添加 `plans.update_task` 工具：修改单个任务
- 添加 `plans.delete_task` 工具：删除单个任务
- 改进 LLM 提示词，让它理解"修改"意图

**实现难度**：中等，需要修改工具注册和 workflow 逻辑

### 2. WebSearch MCP 集成

**现状**：系统没有网络搜索能力，所有工具都是围绕飞书功能。

**建议**：
- 集成 `search-engine-tool-mcp` 或其他搜索 MCP
- 添加 `websearch` 工具定义
- 配置 Policy Guard 限制为只读
- 更新系统提示词，告知有搜索能力

**实现难度**：低，系统架构已支持 MCP 集成

**实现步骤**：
1. 在 `song_agent/mcp/` 目录创建 `websearch.py`
2. 在 `ToolRegistry` 中添加 `websearch` 工具
3. 在 Policy Guard 中配置权限
4. 更新 LLM 系统提示词

### 3. 更多 MCP 工具

**建议**：
- 天气查询 MCP
- 新闻订阅 MCP
- 股票行情 MCP
- 其他第三方服务集成

**架构优势**：系统已支持 MCP 集成，扩展性强

---

## 技术栈

- **语言**：Python 3.11+
- **包管理**：uv
- **Web 框架**：FastAPI / Uvicorn
- **数据库**：SQLite + WAL / aiosqlite
- **HTTP 客户端**：HTTPX
- **数据验证**：Pydantic
- **调度器**：APScheduler
- **飞书集成**：
  - 飞书官方 Python SDK
  - 飞书 OpenAPI
  - 飞书官方 MCP（`@larksuiteoapi/lark-mcp@0.5.1`）
- **LLM**：OpenAI-compatible Chat Completions（支持 GLM-5.2 等模型）
- **MCP**：Model Context Protocol（用于低风险、无用户状态的辅助工具）

---

## 安全边界

- **身份隔离**：按 tenant/app/chat/thread/principal 隔离会话
- **Token 加密**：OAuth access/refresh token 使用 AES-256-GCM 加密存储
- **工具权限**：LLM 只能看到 `local` 和 `prepare` 工具，不能看到 `commit_*` 方法
- **确认机制**：日历和文档写操作需要用户点击确认卡片
- **审计日志**：记录所有操作，但不保存敏感信息（token、完整消息）
- **数据保护**：数据库权限为 `0600`，只有当前用户可读写

---

## 快速开始

```bash
# 安装依赖
uv sync

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入飞书应用配置和 LLM API 密钥

# 运行服务
uv run song-agent --reload

# 验证
uv run ruff check song_agent tests
uv run pytest -q
curl http://127.0.0.1:45837/health
```

---

## 相关文档

- [Song Agent 2.0 迁移指南](./song-agent-2.0.md)
- [README.md](../README.md)