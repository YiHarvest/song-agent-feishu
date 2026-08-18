# Song Agent v1

Song Agent 是一个可插拔的 Intent 执行框架。自然语言只在入口处经过一次 LLM 路由；一旦 Intent 已知，HTTP API、Agent Tool Call、Action 确认和 Scheduler 全部复用同一个安全执行内核。

```text
自然语言 ──> TopLevelRouter ──> IntentDispatcher ──> Handler
已知 Intent ───────────────────> IntentDispatcher ──> Handler
agent.execute ──> Agent Runtime ──> 已知 Intent ──> IntentDispatcher
action_id ──> ActionService ──> 内核确认路径 ──> IntentDispatcher
```

核心原则是：不确定“做什么”时使用 Router；已经知道“做什么”时直接使用 Dispatcher。

## 架构边界

- `song_agent.kernel`：中立的 `PrincipalIdentity`、`Inbound RequestEnvelope`、`DeliveryTarget`、`IntentSpec` 与 `IntentResult`。
- `song_agent.runtime`：Catalog、Router、Dispatcher、Action 状态机、Agent Runtime、模块装配、上下文组合与 Scheduler 触发。
- `song_agent.modules`：通过统一协议贡献 Intent；每个模块只能注册自己拥有的顶级 namespace。
- `song_agent.adapters`：API Key、OpenAI-compatible HTTP、飞书 Channel/Workspace、LLM、附件存储与最终呈现。
- `song_agent.infrastructure`：SQLite Repository 和统一 Alembic Schema。

`IntentCatalog` 是 Intent 元数据的唯一事实来源。Router 的候选列表和 Agent Tool 列表都由 Catalog 动态生成，没有平行的 Intent 枚举或 Prompt 白名单。

## 安全模型

- `PrincipalIdentity` 只能由 API Key 或经过验证的飞书事件推导；请求体不能提交身份。
- Handler 只返回通道无关的 `IntentResult`，不生成飞书卡片、HTML 或通道 Markdown。
- 删除、覆盖和批量操作进入同步 Action 状态机：`PENDING → EXECUTING → SUCCEEDED / FAILED / UNKNOWN`。
- 确认接口只接收 `action_id`。原始 Intent、参数、身份、投递目标、版本、权限和 Payload Hash 均从数据库加载并重新验证。
- `UNKNOWN` 只能由 `ActionService` reconcile；仍无法确认时，仅在 Provider 支持稳定幂等键的策略下复用原幂等键重试。
- 网络调用期间不持有 SQLite 事务。Repository 的 reservation、claim 和状态落库均为独立短事务。
- 附件入口只做可信获取、大小/MIME/Hash 校验、暂存和作用域引用；内容理解由 `attachment.*` Handler 完成。
- 只使用一个独立的 32-byte AEAD 主密钥；本阶段没有轮换、多版本或旧密钥回退。
- 飞书 Workspace 使用按 `tenant_id + principal_id` 绑定的用户 OAuth 凭证；Token 加密落库，OAuth state 单次消费，且服务端语义权限白名单先于 OAuth scope 校验。

## HTTP 面

- `POST /v1/chat/completions`：OpenAI-compatible 自然语言入口，支持普通响应和 SSE streaming。
- `POST /api/v1/intents/{intent_id}/execute`：明确 Intent 入口。
- `/api/v1/actions/{action_id}/...`：查询、确认、取消、reconcile 与安全 retry。
- `GET /v1/models`、`GET /health`：模型与无密钥健康信息。
- `POST /adapters/feishu/events`：飞书可信 Channel Adapter 回调，不是业务 REST API。
- `GET /adapters/feishu/oauth/callback`：飞书用户 OAuth 回调，只恢复服务端持久化的身份与目标，不接受客户端重构执行身份。

第一阶段没有模块级业务 REST Router。后续类型化 REST Adapter 也必须转换为 Intent 并进入 Dispatcher。

## 内置模块

默认启用 `conversation`、`memory`、`agent`、`plan`、`delivery`、`system`。可选模块包括 `calendar`、`task`、`reminder`、`document`、`search` 和 `attachment`。

- Conversation 只管理近期消息与摘要；Memory 单独管理跨会话长期记忆。
- `agent.execute` 进行有限步 Tool Calling，只能调用 `agent_exposed=True` 的 Catalog Intent，并禁止递归调用自己。
- Calendar、Task、Reminder 与 Document 通过中立 Port 调用 Feishu Workspace Adapter。
- `delivery.create_binding` 只能从可信飞书会话创建，API 后续仅引用 binding id。
- Scheduler 使用持久化 lease/fencing，并直接构造 `system.scheduler.broadcast`，不会再次调用 LLM Router。

## 全新数据库

v1 不兼容旧 Schema，也不导入旧 JSON。默认数据库是 `.data/song-agent-v1.db`。迁移只在部署阶段显式执行，应用启动只校验 Alembic revision：

```bash
uv sync
cp .env.example .env
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
uv run song-agent migrate
uv run song-agent serve --reload
```

启用飞书 Workspace 时，`SONG_AGENT_PUBLIC_BASE_URL` 必须是飞书后台已登记、外部可访问的 HTTPS 地址；回调路径固定为 `/adapters/feishu/oauth/callback`。

模块禁用或删除时保留数据；迁移只允许前向执行。

## 验证

```bash
uv run ruff check .
uv run pytest -q
codegraph sync
codegraph explore "Router Dispatcher ActionService Agent Runtime architecture"
```
