# Song Agent 2.1 可行性分析报告

> 日期：2026-07-24  
> 分析版本：基于当前代码实现  
> 参考项目：22 个开源项目

---

## 一、总体判断

Song Agent 2.1 的方向是正确的，**完成度约 75%–80%**：

### ✅ 已完成的核心能力

- ✅ ReAct Runtime 已成为智能决策入口
- ✅ Tool Registry、Policy Guard、Pending Action 的安全分层基本成立
- ✅ OAuth 隔离、Token 加密、SQLite WAL、调度租约、审计等生产能力已进入架构
- ✅ 日历、文档、计划、复盘的产品范围清晰
- ✅ 预算管理、超时控制、上下文裁剪已实现
- ✅ 所有测试通过（54/54）

### ⚠️ 存在的问题

文档和代码中还存在几处明显冲突和缺失：

1. ~~把 OAuth 写成了"OAuth 3.0"~~（已在代码中修正）
2. `workflow.py` 仍被描述成核心处理器，与 ReAct Runtime 冲突
3. MCP 和 OpenAPI 同时承担日历、文档写操作，边界不清
4. APScheduler 3.x API、自建 `scheduled_jobs`、APScheduler 4.x lease 思路混在一起
5. "所有写操作都确认"与 `plans.save_draft`、`reviews.save` 直接写入互相冲突
6. OAuth 授权后自动重放原始请求，还缺少过期、单次执行和内容防篡改设计

---

## 二、参考项目分析

### 2.1 ReAct 智能体主架构

#### nanobot (HKUDS)
**仓库**：https://github.com/HKUDS/nanobot

**参考价值**：⭐⭐⭐⭐⭐
- Agent Loop 设计
- Tool Registry 模式
- 消息总线架构
- Channel 与 Agent 解耦
- Memory 管理
- MCP 接入
- Cron 与提醒
- Agent 步骤限制
- 流式输出
- 飞书 Channel
- 长任务运行状态

**当前状态**：✅ 已实现核心功能
- ReAct Runtime 已形成：LLM 决策 → Tool Call → Tool Result → 再次决策 → Final Answer
- 不是：LLM 分类 → workflow.py 固定分支

**建议**：
- ✅ 保持当前 ReAct 架构
- ⚠️ 需要明确 workflow.py 的定位（见 P0-2）

---

#### LangGraph (langchain-ai)
**仓库**：https://github.com/langchain-ai/langgraph

**参考价值**：⭐⭐⭐⭐
- 持久化 Agent State
- checkpoint 机制
- thread ID 管理
- interrupt/resume
- Human-in-the-loop
- retry 和 timeout
- 长时间运行 Agent 的恢复

**当前状态**：⚠️ 部分实现
- ✅ 有 Agent Run 记录
- ✅ 有 Pending Action 状态机
- ⚠️ 缺少完整的 checkpoint 版本管理
- ⚠️ 缺少 interrupt/resume 机制

**建议**：
- 参考 LangGraph 的状态持久化设计
- 实现 Agent Run 的 checkpoint 版本管理
- 完善 interrupt/resume 机制

---

#### Agents From Scratch (langchain-ai)
**仓库**：https://github.com/langchain-ai/agents-from-scratch

**参考价值**：⭐⭐⭐⭐⭐
- Agent 自主选择工具
- 发送邮件、安排会议等敏感工具需要人工确认
- 确认机制不会把整个 Agent 退化成固定工作流
- 用户反馈可以进入 Memory
- 有工具调用和 Agent 行为测试

**当前状态**：✅ 已实现核心功能
- ✅ Pending Action 状态机
- ✅ 确认机制
- ✅ 工具调用测试

**建议**：
- ✅ 保持当前设计
- 参考 email_assistant_hitl.py 的确认流程

---

### 2.2 飞书 Gateway 与交互卡片

#### Hermes Agent (NousResearch)
**仓库**：https://github.com/NousResearch/hermes-agent/blob/main/gateway/platforms/feishu.py

**参考价值**：⭐⭐⭐⭐⭐
- WebSocket 与 Webhook 两种模式
- 群聊必须 @机器人
- 持久化消息去重
- per-chat 串行处理
- 消息 burst 合并
- 图片、文件、音频处理
- 处理中表情
- 卡片按钮回调
- 用户 allowlist
- Webhook body 限制
- 回调验证
- 断线重连
- open_id、union_id 等身份层次

**当前状态**：✅ 已实现核心功能
- ✅ event_id / message_id 去重
- ✅ tenant + app 作用域
- ✅ chat/thread 串行队列
- ✅ 群聊 mention 策略
- ✅ 断线重连
- ⚠️ 缺少文本 burst 合并
- ⚠️ 缺少入站消息大小限制

**建议**：
- 实现消息 burst 合并（短时间内多条消息合并处理）
- 添加入站消息大小限制

---

#### OpenClaw Lark (larksuite)
**仓库**：https://github.com/larksuite/openclaw-lark

**参考价值**：⭐⭐⭐⭐
- 飞书流式卡片
- 确认、拒绝按钮
- 卡片更新
- Markdown 转卡片
- 群策略
- 日历、文档等工具 Schema
- 飞书权限提示
- 错误信息展示

**当前状态**：✅ 已实现核心功能
- ✅ 交互卡片
- ✅ 确认/拒绝按钮
- ⚠️ 缺少流式卡片更新

**建议**：
- 实现流式卡片更新（实时显示 Agent 运行状态）

---

#### 飞书官方 OpenAPI MCP
**仓库**：https://github.com/larksuite/lark-openapi-mcp

**参考价值**：⭐⭐⭐
- 飞书工具 Schema
- OAuth scope
- `user_access_token` 和 `tenant_access_token` 的区别
- 日历与文档接口参数
- 飞书 API 错误
- MCP 工具命名
- OAuth 登录流程

**当前状态**：⚠️ 定位不清
- ✅ 已实现 MCP 工具
- ⚠️ MCP 和 OpenAPI 边界不清（见 P0-7）

**建议**：
- 明确 MCP 只用于开发调试或低风险辅助
- 核心写操作禁止自动 fallback 到 MCP

---

### 2.3 多用户 OAuth 隔离

#### Slack Bolt Python
**仓库**：https://github.com/slackapi/bolt-python

**参考价值**：⭐⭐⭐⭐⭐
- OAuth state 与 Installation 分开存储
- 每个请求根据 workspace/user 查 token
- 多租户安装
- 用户 token 和机器人 token 区分
- token rotation
- 授权中间件
- 用户 token 不匹配时重新解析

**当前状态**：✅ 已实现核心功能
- ✅ OAuth state 存储
- ✅ 多租户隔离
- ✅ Token 加密存储
- ✅ Token rotation（密钥轮换）

**建议**：
- ✅ 保持当前设计

---

#### Python Slack SDK
**仓库**：https://github.com/slackapi/python-slack-sdk

**参考价值**：⭐⭐⭐⭐
- InstallationStore 数据模型
- client_id、enterprise_id、team_id、user_id
- user_token、user_refresh_token、user_token_expires_at

**当前状态**：✅ 已实现核心功能
- ✅ 类似的数据模型

**建议**：
- ✅ 保持当前设计

---

#### Nango
**仓库**：https://github.com/NangoHQ/nango

**参考价值**：⭐⭐⭐⭐
- OAuth Connection 模型
- 多租户连接
- credentials 加密
- 自动 refresh
- refresh 成功和失败记录
- credentials 过期时间
- 连接禁用和撤销
- connection ID 与用户业务 ID 解耦
- 代理外部 API 请求时自动解析凭据

**当前状态**：✅ 已实现核心功能
- ✅ OAuth Connection 模型
- ✅ credentials 加密
- ✅ 自动 refresh
- ⚠️ 缺少连接禁用和撤销

**建议**：
- 添加连接禁用和撤销功能

---

#### Composio
**仓库**：https://github.com/ComposioHQ/composio

**参考价值**：⭐⭐⭐
- user_id、auth_config_id、connected_account_id
- account alias
- multiple accounts
- connection status

**当前状态**：✅ 已实现核心功能
- ✅ principal_id → OAuth connection → Agent Tool Context

**建议**：
- ✅ 保持当前设计

---

### 2.4 Token 加密

#### pyca/cryptography
**仓库**：https://github.com/pyca/cryptography

**参考价值**：⭐⭐⭐⭐⭐
- Fernet 对称加密
- MultiFernet 多密钥支持
- authenticated encryption
- key rotation
- 多密钥解密
- 安全随机数
- AES-GCM 等低层原语

**当前状态**：✅ 已实现核心功能
- ✅ 使用 Fernet 加密
- ✅ 支持多版本密钥
- ✅ 每次加密使用随机 nonce
- ✅ 保存 authentication tag
- ✅ 支持旧密钥解密
- ✅ 支持新密钥写入
- ✅ 禁止解密失败后退回明文
- ✅ 彻底清理日志中的 token

**建议**：
- ✅ 保持当前设计

---

### 2.5 Pending Action 与人工确认

#### LangGraph Human-in-the-loop
**仓库**：https://github.com/langchain-ai/langgraph

**参考价值**：⭐⭐⭐⭐⭐
- 只对指定敏感工具中断
- 用户可以确认、编辑、拒绝
- Agent 状态能够持久化
- 恢复后继续执行
- 确认由确定性策略触发，而不是提示词决定

**当前状态**：⚠️ 部分实现
- ✅ 有 Pending Action 状态机
- ✅ 有确认机制
- ⚠️ 缺少完整的状态定义（见 P0-10）

**建议**：
- 完善状态定义：
  ```
  PROPOSED
  AWAITING_CONFIRMATION
  CONFIRMED
  CLAIMED
  EXECUTING
  REMOTE_SUCCEEDED
  SUCCEEDED
  FAILED_RETRYABLE
  FAILED_FINAL
  UNKNOWN_REMOTE_STATE
  CANCELLED
  EXPIRED
  ```

---

### 2.6 外部操作幂等和恢复

#### Temporal
**仓库**：https://github.com/temporalio/temporal

**参考价值**：⭐⭐⭐⭐
- Workflow 与 Activity 分离
- 外部副作用放入 Activity
- Activity retry
- timeout
- heartbeat
- durable state
- Saga compensation
- signal
- 外部操作结果恢复
- Pydantic payload

**当前状态**：⚠️ 部分实现
- ✅ 有 Pending Action 状态机
- ✅ 有 reconciliation 服务
- ⚠️ 缺少完整的 Saga compensation

**建议**：
- 参考 Temporal 的 Activity retry 设计
- 实现完整的 Saga compensation

---

#### Transactional Outbox Pattern
**仓库**：https://github.com/aws-samples/transactional-outbox-pattern

**参考价值**：⭐⭐⭐⭐⭐
- 业务数据和 outbox 记录在同一个事务里写入
- 消费者仍然必须支持重复消息

**当前状态**：✅ 已实现核心功能
- ✅ 有 outbox 服务
- ⚠️ 缺少区分 action_outbox 和 message_outbox

**建议**：
- 区分两张表：
  ```
  action_outbox：飞书 API 执行任务
  message_outbox：飞书回复消息
  ```

---

### 2.7 Scheduler 与 Leader Lease

#### APScheduler
**仓库**：https://github.com/agronholm/apscheduler

**参考价值**：⭐⭐⭐
- schedule/job 分离
- `acquired_by`、`acquired_until`
- lease extension
- abandoned job recovery
- misfire
- retry
- persistent datastore

**当前状态**：⚠️ 存在冲突（见 P0-9）
- ✅ 使用 APScheduler 3.x
- ✅ 有自建的 scheduled_jobs 表
- ✅ 有 scheduler_leases 表
- ⚠️ APScheduler 4.x 仍是预发布版本

**建议**：
- 采用方案 A：当前更稳妥
  ```
  APScheduler 3.x
  + 单 scheduler 进程
  + Song Agent 自己的 scheduled_jobs
  + Song Agent 自己的 scheduler_leases
  ```
- scheduled_jobs 是唯一事实源
- APScheduler 只负责进程内触发

---

#### Kubernetes client-go
**仓库**：https://github.com/kubernetes/client-go

**参考价值**：⭐⭐⭐⭐
- LeaseDuration
- RenewDeadline
- RetryPeriod
- holder identity
- lease 到期接管
- 失去 leader 后停止工作
- ReleaseOnCancel

**当前状态**：✅ 已实现核心功能
- ✅ 有 scheduler_leases 表
- ✅ 有 fencing_token
- ⚠️ 缺少完整的 lease 到期接管逻辑

**建议**：
- 实现完整的 lease 到期接管逻辑

---

#### Prefect
**仓库**：https://github.com/PrefectHQ/prefect

**参考价值**：⭐⭐⭐
- worker heartbeat
- zombie task 检测
- lease renewal
- scheduled run polling
- retry
- 崩溃状态
- worker identity

**当前状态**：✅ 已实现核心功能
- ✅ 有 scheduler lease
- ✅ 有 fencing_token

**建议**：
- 参考 Prefect 的 worker heartbeat 设计

---

### 2.8 审计日志和可观测性

#### django-auditlog
**仓库**：https://github.com/jazzband/django-auditlog

**参考价值**：⭐⭐⭐⭐
- actor
- action
- resource
- old/new changes
- timestamp
- correlation ID
- additional metadata
- 敏感字段 mask
- create/update/delete/access 分类

**当前状态**：✅ 已实现核心功能
- ✅ 有 audit 服务
- ✅ 有敏感信息脱敏
- ⚠️ 缺少 old/new changes

**建议**：
- 添加 old/new changes 记录

---

#### starlette-context
**仓库**：https://github.com/tomwojcik/starlette-context

**参考价值**：⭐⭐⭐⭐
- Request ID
- Correlation ID
- ContextVar
- FastAPI/Starlette middleware
- 日志自动注入 trace 上下文

**当前状态**：✅ 已实现核心功能
- ✅ 有 observability/context.py
- ✅ 有 trace_scope

**建议**：
- ✅ 保持当前设计

---

#### OpenTelemetry Python
**仓库**：https://github.com/open-telemetry/opentelemetry-python

**参考价值**：⭐⭐⭐⭐
- trace/span
- FastAPI instrumentation
- HTTPX instrumentation
- logging correlation
- background task tracing
- SQLite instrumentation
- 自定义 Agent/tool spans

**当前状态**：⚠️ 未集成
- ⚠️ 缺少 OpenTelemetry 集成

**建议**：
- 集成 OpenTelemetry
- 实现 Span 层次：
  ```
  feishu.message
  └── agent.run
      ├── llm.decide
      ├── tool.calendar.list
      ├── pending_action.prepare
      └── feishu.reply
  ```

---

### 2.9 MCP 扩展

#### MCP Python SDK
**仓库**：https://github.com/modelcontextprotocol/python-sdk

**参考价值**：⭐⭐⭐⭐
- MCP Client
- stdio
- Streamable HTTP
- lifecycle
- structured output
- timeout
- request ID
- tool result 解析
- OAuth client

**当前状态**：✅ 已实现核心功能
- ✅ 有 MCP 集成
- ⚠️ 需要固定版本：`mcp = ">=1.27,<2"`

**建议**：
- 固定 MCP SDK 版本，避免不兼容变化

---

#### Search Engine MCP
**仓库**：https://github.com/YiHarvest/search-engine-tool

**参考价值**：⭐⭐⭐
- 多个搜索/内容提取 Provider

**当前状态**：⚠️ 未集成

**建议**：
- 接入时必须作为：
  ```
  READ_ONLY
  UNTRUSTED_OUTPUT
  ```
- 增加安全措施：
  - 请求超时
  - 返回长度限制
  - URL allow/deny
  - SSRF 防护
  - 私网 IP 阻止
  - Prompt Injection 提示
  - 不向搜索 MCP 传 token、用户文档、日历内容
  - 搜索结果不能直接触发 commit 工具

---

## 三、需要立即修改的问题（P0）

### P0-1：OAuth 3.0 名称错误 ✅ 已修正

**问题**：文档中写成了"OAuth 3.0"

**状态**：✅ 已在代码中修正为"OAuth 2.0"

---

### P0-2：ReAct Runtime 和 workflow.py 权力冲突 ⚠️ 需要修改

**问题**：文档同时说：
- runtime.py：ReAct 决策引擎
- workflow.py：核心工作流处理器

**建议**：
- 明确 runtime.py 是唯一自然语言决策中心
- workflow.py 只负责：
  - 构建 AgentContext
  - 调用快速路由
  - 调用 AgentRuntime
  - 渲染 AgentResult
  - 发送飞书消息

**推荐重命名**：
```
workflow.py → chat_orchestrator.py
```

---

### P0-3：快速路径不能成为第二套关键词路由 ⚠️ 需要修改

**问题**：当前快速路径可能包含：
```
今天有什么任务
```

**建议**：
- 快速路径只包含：
  - `/help`
  - `/status`
  - 授权状态
  - 简单问候
- 其余自然语言全部进入 ReAct

---

### P0-4：不要过度强调 `final_answer` ⚠️ 需要修改

**问题**：当前提示词：
```
优先使用 final_answer，避免不必要的工具调用
```

**建议**：改成：
```
当答案不依赖外部状态、无需执行动作时，使用 final_answer。
当用户要求查询真实数据或执行操作时，必须调用对应工具。
不得声称已完成未实际执行的操作。
```

---

### P0-5：工具分类不完整 ⚠️ 需要修改

**问题**：当前只有：
```
local
prepare
commit
```

**建议**：改为：
```
READ
LOCAL_WRITE
EXTERNAL_PREPARE
INTERNAL_COMMIT
SYSTEM
```

**示例**：
| 工具 | 分类 |
|------|------|
| `plans.get_today` | READ |
| `calendar.list_events` | READ |
| `plans.save_draft` | LOCAL_WRITE |
| `reviews.save` | LOCAL_WRITE |
| `calendar.prepare_create_event` | EXTERNAL_PREPARE |
| `calendar.commit_create_event` | INTERNAL_COMMIT |
| `oauth.refresh` | SYSTEM |

---

### P0-6：Policy Guard 不能只返回 bool ⚠️ 需要修改

**问题**：当前：
```python
async def check_permission(...) -> bool
```

**建议**：
```python
class PolicyDecision(BaseModel):
    outcome: Literal[
        "ALLOW",
        "DENY",
        "REQUIRE_CONFIRMATION",
        "REQUIRE_OAUTH",
    ]
    risk_level: Literal[
        "READ",
        "LOW_WRITE",
        "HIGH_WRITE",
        "DESTRUCTIVE",
    ]
    reason: str
    required_scopes: list[str]
```

---

### P0-7：MCP 和 OpenAPI 写操作重复 ⚠️ 需要修改

**问题**：日历、文档实现同时写着：
```
feishu/openapi.py
feishu/mcp.py
```

**建议**：
- 明确规定：
  ```
  日历读取/写入：OpenAPI
  文档读取/写入：OpenAPI
  MCP：开发调试或低风险辅助
  ```
- 核心写操作禁止自动 fallback 到 MCP

---

### P0-8：OAuth 自动继续原始请求缺少安全条件 ⚠️ 需要修改

**问题**：`oauth_authorizations.original_request` 不能授权完成后无条件重放

**建议**：必须同时保存：
```
authorization_id
message_id
tenant_key
app_id
principal_id
chat_id
thread_id
original_request_hash
expires_at
consumed_at
resume_status
```

**恢复条件**：
1. 授权用户与发起用户一致
2. 原请求未过期
3. 未执行过
4. message ID 匹配
5. hash 未变化
6. 原请求如果产生写操作，仍然进入 Pending Action
7. OAuth 完成不等于自动确认写操作

---

### P0-9：Scheduler 只能有一个事实源 ⚠️ 需要修改

**问题**：当前容易形成：
```
APScheduler JobStore
+
scheduled_jobs
+
scheduler_leases
```

**建议**：采用方案 A：
```
scheduled_jobs 是唯一事实源
APScheduler 3.x 只负责进程内触发
scheduler lease 保证单 scheduler
```

**启动时**：
```
从 scheduled_jobs 读取
→ 重新装载 APScheduler
```

---

### P0-10：SQLite 必须写清部署边界 ⚠️ 需要修改

**问题**：文档未明确 SQLite 的部署边界

**建议**：明确：
```
Song Agent 2.1 当前生产边界：单机部署。

Web Worker 可以多个；
Feishu Gateway 单实例；
Scheduler 单 Leader；
SQLite 文件不得放在 NFS 共享盘。
```

---

## 四、需要补齐的功能（第二批）

### 4.1 identity.py

**职责**：
```
open_id
union_id
user_id
principal_id
identity aliases
conversation key
```

**原因**：不能让 `open_id` 直接成为所有业务表的永久用户主键

---

### 4.2 oauth_tokens.py

**职责**：
```
读取
解密
刷新
刷新锁
失效
重新授权
生成不可变 UserTokenContext
```

**原因**：`refresh_token` 不应进入 AgentContext

---

### 4.3 calendar.py / documents.py

**职责**：
```
权限
业务校验
幂等
风险
补偿
审计
```

**原因**：`openapi.py` 只做 HTTP，Service 才做业务逻辑

---

### 4.4 ActionExecutor

**职责**：
```
PendingActionService：创建、确认、取消
ActionExecutor：claim、执行、提交结果
ReconciliationService：处理未知远程状态
```

**原因**：Pending Action 的准备与远程执行不要放在一个类里

---

### 4.5 UNKNOWN_REMOTE_STATE

**问题**：当前缺少完整的状态定义

**建议**：添加：
```
UNKNOWN_REMOTE_STATE：远程成功、本地失败
```

---

### 4.6 SQLite 部署边界

**问题**：文档未明确 SQLite 的部署边界

**建议**：明确单机部署的限制

---

## 五、体验增强功能（第三批）

### 5.1 Gateway 消息合并

**功能**：短时间内多条消息合并处理

**价值**：提高用户体验，减少重复处理

---

### 5.2 流式卡片

**功能**：实时显示 Agent 运行状态

**价值**：提高用户体验，减少等待焦虑

---

### 5.3 WebSearch MCP

**功能**：集成搜索能力

**价值**：扩展 Agent 能力，支持信息检索

---

### 5.4 更多提醒工具

**功能**：区分日历提醒和机器人提醒

**价值**：满足不同场景需求

---

### 5.5 任务增量修改

**功能**：支持任务的增量修改

**价值**：提高用户体验，减少重复创建

---

## 六、修改优先级

### 第一批必须改（P0）

1. ✅ OAuth 3.0 命名（已修正）
2. ⚠️ workflow.py 权限下放
3. ⚠️ MCP 退出核心写路径
4. ⚠️ Scheduler 唯一事实源
5. ⚠️ OAuth 自动恢复安全
6. ⚠️ PolicyDecision 结构化

---

### 第二批补齐

1. identity.py
2. oauth_tokens.py
3. calendar/documents Service
4. ActionExecutor
5. UNKNOWN_REMOTE_STATE
6. SQLite 部署边界

---

### 第三批体验增强

1. Gateway 消息合并
2. 流式卡片
3. WebSearch MCP
4. 更多提醒工具
5. 任务增量修改

---

## 七、Claude 最终核对清单

让 Claude 按下面顺序审查代码：

```
1. ✅ ReAct Runtime 是否为唯一自然语言决策中心
2. ⚠️ workflow.py 是否仍然在做关键词分流
3. ⚠️ LLM 是否能看到 commit 工具
4. ⚠️ Policy Guard 是否返回结构化决策
5. ✅ 每次用户级 OpenAPI 是否显式传 UserTokenContext
6. ✅ refresh_token 是否可能进入 AgentContext 或日志
7. ⚠️ MCP 是否仍在核心日历/文档写路径
8. ⚠️ OAuth 恢复原请求是否有过期、hash、单次消费
9. ⚠️ Pending Action 是否有 UNKNOWN_REMOTE_STATE
10. ⚠️ 远程成功、本地失败后是否可以 reconciliation
11. ⚠️ scheduled_jobs 是否是唯一调度事实源
12. ✅ 旧 leader 提交结果时是否校验 fencing_token
13. ⚠️ 卡片确认是否验证 principal、tenant、app、hash、expiry、status
14. ✅ audit 是否彻底排除 token、code、Authorization header
15. ✅ SQLite 是否使用 foreign_keys、busy_timeout、BEGIN IMMEDIATE
16. ⚠️ Gateway 是否完成事件去重、thread 隔离、per-chat 顺序和重连
17. ✅ Agent 是否有最大步骤、最大工具数、总超时和重复工具检测
18. ⚠️ 快速路径是否只包含确定性、安全、低风险请求
```

---

## 八、总结

### 8.1 完成度评估

- **核心架构**：75%–80%
- **生产能力**：80%–85%
- **用户体验**：60%–70%
- **文档完整性**：70%–75%

### 8.2 主要风险

1. **架构冲突**：workflow.py 和 ReAct Runtime 权力不清
2. **安全风险**：OAuth 自动恢复缺少安全条件
3. **数据一致性**：Scheduler 有多个事实源
4. **边界不清**：MCP 和 OpenAPI 写操作重复

### 8.3 建议行动

1. **立即修改**：P0 问题（1-2 周）
2. **补齐功能**：第二批功能（2-4 周）
3. **体验增强**：第三批功能（4-8 周）

### 8.4 最终判断

Song Agent 2.1 的方向是正确的，核心架构已经成型，但还需要解决几个关键问题才能达到生产就绪状态。建议按照优先级逐步修改，确保每一步都经过充分测试。