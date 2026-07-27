# Song Agent：确定性业务执行 + 开放式 Agent

Song Agent 是基于 Python 与 FastAPI 实现的多用户飞书智能助手。自然语言先经过一次结构化意图提取；日历等确定性业务进入应用服务、PendingAction、Outbox 和 Executor，开放式对话才进入有限步数 ReAct Runtime。

## 核心功能

- 📅 **日程管理**：查询、创建、修改和删除个人日程
- ✅ **任务与提醒**：飞书任务 CRUD；提醒作为带来源标记的个人日程管理
- 📋 **每日计划**：制定和复盘每日任务，支持优先级和时间安排
- 📝 **文档协作**：创建和编辑飞书云文档
- 🔍 **网络搜索**：集成 You.com 和 Tavily 搜索引擎，支持实时信息查询
- 🔐 **安全可控**：OAuth 2.0 多用户隔离，敏感操作需要确认
- 🤖 **智能对话**：普通对话和开放分析使用 ReAct

## 核心安全边界

- 身份和会话按 tenant/app/chat/thread/principal 隔离。
- OAuth access/refresh token 使用 AES-256-GCM 加密后存入 SQLite，数据库启用 WAL。
- `UserTokenContext` 只含短生命周期 access token，不含 refresh token。
- LLM 只提取意图和业务字段，不能决定授权、确认、执行器或卡片结构。
- 日历写操作先创建持久化 PendingAction，再由创建者在飞书确认。
- 飞书交互卡片只使用 `schema: "2.0"`；卡片只携带动作名和 `action_id`，完整业务参数以数据库为准。
- 确认与 Outbox 同事务，执行前原子 claim，并记录 action attempt。
- Outbox 由独立消费者恢复；远端结果不确定时进入 UNKNOWN，不会盲目重试。
- Scheduler 的 job、重试和下一运行时间持久化，并使用 SQLite leader lease 与 fencing token。
- Audit log 记录 trace/action/result/hash，不保存 token、完整消息、完整文档或隐藏思维链。
- Agent run/step 只记录决策摘要、参数 hash/shape 和结果摘要。
- 上下文分为 Request、Business、Conversation、Summary、Memory、Retrieved 六层；
  原始消息永久保留，结构化摘要和长期记忆分别持久化。
- 大型工具结果存入 `tool_results`，Agent 上下文只保留摘要和 `result_ref`。

## 技术栈

- Python 3.11+、uv
- FastAPI / Uvicorn
- SQLite + WAL / aiosqlite
- HTTPX / Pydantic
- APScheduler
- 飞书官方 Python SDK与飞书 OpenAPI
- OpenAI-compatible Chat Completions
- MCP 仅保留给低风险、无用户状态的辅助工具

## 飞书应用配置

建议权限至少包括：

- 应用身份：`im:message:send_as_bot`、`im:message.group_at_msg:readonly`、`im:message.p2p_msg:readonly`
- 用户身份：`calendar:calendar`、`calendar:calendar:readonly`、
  `calendar:calendar.event:create`、`calendar:calendar.event:read`、
  `task:task:read`、`task:task:write`、`docx:document`、`drive:drive`、
  `search:docs:read`、`offline_access`
- 长连接事件：`im.message.receive_v1`
- 卡片回调：`card.action.trigger`

公网回调：

```text
https://你的域名/oauth/callback
https://你的域名/feishu/card/action
```

## 安装与运行

```bash
uv sync
cp .env.example .env
uv run song-agent --reload
```

开发环境可使用 ngrok：

```bash
ngrok http 45837
```

将公网地址写入：

```bash
PUBLIC_BASE_URL=https://xxxx.ngrok-free.app
```

Token 加密建议配置独立主密钥：

```bash
uv run python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

把输出保存为 `SONG_AGENT_TOKEN_KEY_V1`，并设置：

```bash
SONG_AGENT_TOKEN_ACTIVE_KEY_VERSION=1
```

密钥轮换时同时保留旧密钥，新增 V2 并执行：

```bash
uv run song-agent-rotate-keys
```

未配置独立密钥时，开发环境会使用飞书 App Secret 派生兼容密钥；生产环境不建议依赖该回退。

## 使用方式

```text
@宋管家 帮我整理今天的计划
@宋管家 十分钟后提醒我开会
@宋管家 创建一份项目进展文档
@宋管家 把这段内容追加到项目方案
@宋管家 复盘今天的任务
```

外部写操作会展示确认卡片。只有原发起者点击确认后，确定性 Executor 才会调用飞书 OpenAPI。

保留的精确命令：

- `/help`
- `/status`
- `/clear`

## 验证

```bash
uv run ruff check song_agent tests
uv run pytest -q
curl http://0.0.0.0:45837/health
```

数据保存在 `.data/song-agent.db`，权限为 `0600`。旧 `.data/state.json` 仅在首次迁移时读取，迁移后不会继续写入。

当前 Web、飞书 Gateway 和 Scheduler 仍装配在同一进程。Scheduler 已支持多实例选主，但飞书长连接 Gateway 应保持单实例；需要多个 Web worker 时应先把 Gateway 拆成独立进程。

## 飞书 CLI

本机 CLI 位于 `/home/yqy/.local/bin/lark-cli`。涉及飞书资源调试或人工运维时优先使用 CLI，并遵守其 dry-run、用户身份和高风险确认门禁；CLI 用户凭据不会被 Song Agent 运行时复用。

临时取消代理运行：

```bash
source /home/yqy/Projects/song-agent/.venv/bin/activate
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
uv run song-agent  --reload
```
