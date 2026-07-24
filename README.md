# Song Agent 2.0：ReAct 多用户飞书个人管家

Song Agent 2.0 是 Python + FastAPI 实现的多用户飞书智能体。自然语言请求进入有限步数 ReAct Runtime，由 Tool Registry 和 Policy Guard 控制工具选择；所有用户级飞书调用显式携带当前用户的 `UserTokenContext`。

## 核心安全边界

- 身份和会话按 tenant/app/chat/thread/principal 隔离。
- OAuth access/refresh token 使用 AES-256-GCM 加密后存入 SQLite，数据库启用 WAL。
- `UserTokenContext` 只含短生命周期 access token，不含 refresh token。
- LLM 只能看到 read/local/prepare 工具，不能看到任何 `commit_*` 内部执行方法。
- 日历和文档写操作先创建持久化 Pending Action，再由创建者点击飞书卡片确认。
- 卡片只携带 `action_id` 和 `payload_hash`；完整业务参数以数据库为准。
- 确认与 Outbox 同事务，执行前原子 claim，并记录 action attempt。
- Outbox 由独立消费者恢复；远端结果不确定时进入 UNKNOWN，不会盲目重试。
- Scheduler 的 job、重试和下一运行时间持久化，并使用 SQLite leader lease 与 fencing token。
- Audit log 记录 trace/action/result/hash，不保存 token、完整消息、完整文档或隐藏思维链。
- Agent run/step 只记录决策摘要、参数 hash/shape 和结果摘要。

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
- 用户身份：`calendar:calendar`、`calendar:calendar:readonly`、`docx:document`、`drive:drive`、`search:docs:read`、`offline_access`
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
curl http://127.0.0.1:45837/health
```

数据保存在 `.data/song-agent.db`，权限为 `0600`。旧 `.data/state.json` 仅在首次迁移时读取，迁移后不会继续写入。

当前 Web、飞书 Gateway 和 Scheduler 仍装配在同一进程。Scheduler 已支持多实例选主，但飞书长连接 Gateway 应保持单实例；需要多个 Web worker 时应先把 Gateway 拆成独立进程。

## 飞书 CLI

本机已安装官方 `lark-cli` v1.0.76。涉及飞书资源调试或人工运维时优先使用 CLI，并遵守其 dry-run、用户身份和高风险确认门禁；CLI 尚未绑定用户账号，Song Agent 的生产多用户运行时也不会共享 CLI 用户凭据。

# 临时取消代理运行
source /home/yqy/Projects/song-agent/.venv/bin/activate
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
uv run song-agent
