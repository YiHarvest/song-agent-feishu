# Song Agent 2.0 逐文件改造清单

## 改造原则

- FastAPI、现有计划/复盘业务和 Python 技术栈保留。
- 所有用户级飞书调用必须显式携带不可变的 `UserTokenContext`。
- 生产核心写操作逐步迁移到飞书 OpenAPI；MCP 只保留为低风险、无状态工具。
- 持久化统一使用 SQLite + WAL；唯一约束和事务负责并发安全。
- LLM 运行在有限步数 ReAct Runtime 中，只能选择 Tool Registry 的白名单工具。
- 敏感操作使用持久化 `pending_action` 和交互卡片，不再依赖裸文本“确认”。

## 现有文件

| 文件 | 处理 | 内容 |
|---|---|---|
| `song_agent/__init__.py` | 保留并修改 | 版本升级到 0.3.x。 |
| `song_agent/__main__.py` | 保留 | 继续作为 Uvicorn 入口；后续增加 worker/调度器单实例保护说明。 |
| `song_agent/app.py` | 重写 | 只负责依赖装配和生命周期；注入 SQLite、Gateway、Router、Services、OpenAPI Adapter。 |
| `song_agent/config.py` | 重写 | 新增 `DATABASE_PATH`、事件保留期、卡片回调配置；`DATA_FILE` 仅作为旧 JSON 迁移源。 |
| `song_agent/llm.py` | 保留并强化 | 保留 OpenAI 兼容调用；增加结构化输出错误分类、重试和超时策略。 |
| `song_agent/models.py` | 重写 | 增加身份、会话键、结构化意图、TokenContext、PendingAction、审计模型。 |
| `song_agent/planner.py` | 拆分并重写 | 保留计划/复盘格式化；删除危险的关键词优先意图路由，改为严格结构化分类。 |
| `song_agent/scheduler/` | 重写 | APScheduler 运行时增加 SQLite leader lease 与 fencing token。 |
| `song_agent/store.py` | 完全重写 | `JsonStore` 替换为 `SqliteStore`，启用 WAL、外键、事务、唯一约束和旧 JSON 一次性迁移。 |
| `song_agent/workflow.py` | 拆分并重写 | 只做编排；动作执行、确认、权限和幂等分别下沉到 Service。 |
| `song_agent/feishu/transport.py` | 重写 | 作为 Gateway：事件规范化、身份提取、线程信息、原子去重、per-chat 串行队列、重连状态。 |
| `song_agent/feishu/oauth.py` | 重写 | OAuth state 持久化；token 按 tenant/app/subject 隔离；refresh 使用 installation lease；返回 TokenContext。 |
| `song_agent/feishu/mcp.py` | 降级后删除核心写路径 | 日历/文档写操作迁出；最终仅保留无用户状态、低风险 MCP 工具。 |
| `tests/test_store.py` | 重写 | 改测 SQLite WAL、唯一约束、并发 claim、JSON 迁移和多用户 token 隔离。 |
| `tests/test_planner.py` | 重写 | 增加否定句和歧义句，确保关键词不会直接触发写操作。 |
| `tests/test_transport.py` | 扩充 | 增加 tenant/app/thread/union_id 规范化、重复事件和 per-chat 顺序测试。 |
| `README.md` | 保留并后续更新 | 保留当前用户修改；架构稳定后更新部署、迁移和安全说明。 |
| `.env.example` | 修改 | 增加数据库、卡片回调和迁移配置；标记 MCP 不用于生产核心写操作。 |
| `pyproject.toml` / `uv.lock` | 修改 | 增加 `aiosqlite`；删除核心路径不再需要的 MCP 依赖后再清理锁文件。 |
| `.data/state.json` | 迁移后停用 | 首次启动导入 SQLite；不删除原文件，迁移成功后记录校验摘要。 |

## 新增文件

| 文件 | 用途 |
|---|---|
| `song_agent/identity.py` | 身份解析、稳定 subject 选择和会话键生成。 |
| `song_agent/feishu/openapi.py` | 无状态 HTTPX 飞书 OpenAPI Adapter；每个方法显式接收 TokenContext。 |
| `song_agent/services/calendar.py` | 日历业务、幂等键、权限和失败补偿。 |
| `song_agent/services/documents.py` | 文档创建/追加/search 业务。 |
| `song_agent/services/pending_actions.py` | 草稿摘要、payload hash、过期、确认/取消和 effectively-once claim。 |
| `song_agent/services/outbox.py` | 持久化确认动作的独立消费者；服务重启后继续执行。 |
| `song_agent/services/reconciliation.py` | 根据已持久化的远端资源 ID 修复本地状态；无证据时保持 UNKNOWN。 |
| `song_agent/services/encryption.py` | AES-256-GCM、AAD、密钥版本和轮换。 |
| `song_agent/services/agent_runs.py` | 记录 Agent run/step 摘要，不保存完整思维链或原始参数。 |
| `song_agent/db/migrations.py` | 有序 SQLite schema/data migrations。 |
| `song_agent/services/oauth_tokens.py` | token 获取、刷新锁和失效处理。 |
| `song_agent/router.py` | 精确命令 + LLM 结构化意图 + Pydantic 校验。 |
| `song_agent/feishu/cards.py` | 确认/修改/取消卡片和回调解析。 |
| `tests/test_identity.py` | 身份优先级和线程会话隔离。 |
| `tests/test_pending_actions.py` | 发送者、hash、过期和重复点击安全性。 |
| `tests/test_openapi.py` | HTTP mock 下验证 token 不串用户和幂等头/参数。 |
| `tests/test_gateway.py` | 去重、顺序、群策略、断线重投。 |

## 实施顺序

1. SQLite/WAL、旧 JSON 迁移、原子事件 claim。
2. 身份模型、线程会话键、显式 `UserTokenContext`、refresh lock。
3. LLM 结构化路由，删除关键词优先写操作。
4. `pending_actions` + 飞书交互卡片 + effectively-once 执行。
5. 日历和文档改走 OpenAPI Adapter，MCP 降级。
6. Gateway 流式卡片、消息合并、重连和群策略。
7. 持久化调度、审计日志、故障注入和并发测试。

## 当前进度

- [x] SQLite + WAL schema，数据库权限 `0600`。
- [x] 旧 `.data/state.json` 单次、校验摘要式导入，原文件保留。
- [x] 原子事件 claim，作用域包含 tenant/app。
- [x] tenant/app/chat/thread/subject 会话隔离键和 per-chat/thread 串行锁。
- [x] OAuth token 按 tenant/app/subject 隔离；refresh 使用跨进程 installation lease、失败状态和 token version。
- [x] OAuth state 持久化、哈希存储、单次消费。
- [x] 自然语言入口升级为 ReAct Runtime + Tool Registry + Policy Guard。
- [x] Agent 限制最大步数、工具次数、连续错误、超时和重复工具调用。
- [x] LLM 不可见任何 `commit_*` 工具，不保存完整隐藏思维过程。
- [x] 日历创建迁移到直接 OpenAPI，并使用显式 `UserTokenContext` 和幂等键。
- [x] 日历确认改为持久化 action + 交互卡片；验证发送者、hash、过期和重复点击。
- [x] OAuth access/refresh token 使用 AES-256-GCM 加密，支持明文在线迁移和密钥版本轮换。
- [x] `UserTokenContext` 已移除 refresh token。
- [x] 文档 search/read/create/append 迁移到直接 OpenAPI；写操作纳入交互确认。
- [x] action confirmation + Outbox 同事务；独立消费者可在重启后恢复 confirmed action。
- [x] action attempt 记录远端成功证据；租约过期或结果不确定时进入 `UNKNOWN_REMOTE_STATE`，禁止盲重试。
- [x] reconciliation 可依据已持久化的文档 ID/日历事件映射补完本地事务；无证据时保持 UNKNOWN。
- [x] APScheduler 广播增加 SQLite leader lease 与 fencing token。
- [x] `scheduled_jobs` 声明式持久化、missed-run、claim、指数退避和重启恢复。
- [x] audit log v2、trace context 与敏感字段/正文脱敏。
- [x] `agent_runs` / `agent_steps` 持久化；仅保存决策摘要、参数 shape/hash、结果摘要和最终答复。
- [x] 飞书 SDK 使用独立线程事件循环；连接日志不再打印含 ticket 的 WebSocket URL。
- [x] 官方 `lark-cli` v1.0.76 已安装；飞书调试操作优先使用 CLI。
- [ ] Gateway 消息合并、流式卡片、群策略配置和断线故障测试。
- [ ] 对“远端成功但本地尚未获得资源 ID”的 UNKNOWN 动作，增加飞书资源搜索/业务指纹核对和管理员处理界面。
- [ ] 增加 OpenTelemetry exporter；当前已具备 trace context、X-Trace-ID 和持久化 audit。
