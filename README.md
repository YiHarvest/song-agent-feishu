# Song Agent：飞书多用户个人管家

Python + uv + FastAPI 实现的飞书自建 Agent。它通过飞书长连接接收群聊艾特，用 llm 整理计划、提醒、复盘和文档，并通过飞书官方 MCP 操作每位用户自己的日历与云空间。

## 核心保证

- 群聊内所有成员都可艾特机器人，不使用单一用户白名单拦截。
- 计划记录按 `chat_id + open_id + date` 隔离，多人不会互相覆盖。
- OAuth token 按用户 `open_id` 独立保存；每次 MCP 调用只传当前消息发送者的 token。
- 日程通过当前用户的主日历创建，文档导入当前用户的云空间，不会统一写到群主或开发者账号。
- 日程必须由原创建者精确回复“确认”后才会写入。

## 技术栈

- Python 3.11+
- uv
- FastAPI / Uvicorn
- 飞书官方 Python SDK `lark-oapi`（WebSocket 长连接与消息）
- Python MCP client + 飞书官方 `@larksuiteoapi/lark-mcp` 独立工具进程
- GLM-5 的 OpenAI-compatible Chat Completions 接口
- Pydantic / HTTPX / APScheduler

项目没有 TypeScript 业务代码。飞书官方 MCP 自身由 Node.js 运行，它是独立的上游工具服务，不属于本项目技术栈。

## 飞书应用配置

应用需要：

- 应用身份：`im:message:send_as_bot`、`im:message.group_at_msg:readonly`、`im:message.p2p_msg:readonly`
- 用户身份：`calendar:calendar`、`calendar:calendar:readonly`、`docx:document`、`offline_access`
- 长连接事件：`im.message.receive_v1`
- OAuth 回调：`http://127.0.0.1:45837/oauth/callback`（部署时改为公网 HTTPS，目前使用的是内网穿透）

机器人加入目标群后，管理员第一次艾特会持久化绑定该群；随后该群所有成员均可使用。生产环境也可以直接填写 `FEISHU_ALLOWED_GROUP_CHAT_IDS`。

## 安装与运行

```bash
uv sync
cp .env.example .env
```

### 内网穿透（开发环境）

OAuth 授权需要公网回调地址。开发环境推荐使用 ngrok 内网穿透：

```bash
# 启动 ngrok（需要先注册 ngrok 账号并配置 authtoken）
ngrok http 45837

# 复制 ngrok 提供的公网地址，例如：
# https://xxxx.ngrok-free.app
```

修改 `.env` 中的 `PUBLIC_BASE_URL`：

```bash
PUBLIC_BASE_URL=https://xxxx.ngrok-free.app
```

同时在飞书开放平台添加 OAuth 回调地址：

```
https://xxxx.ngrok-free.app/oauth/callback
```

### 启动服务

```bash
uv run song-agent
```

开发时启用代码变更自动重载：

```bash
uv run song-agent --reload
```

飞书 MCP 默认按官方方式通过 `npx -y @larksuiteoapi/lark-mcp@0.5.1` 作为独立进程运行；也可单独安装官方包，再通过 `FEISHU_MCP_CLI` 指向其 `dist/cli.js`。

健康检查：

```bash
curl http://127.0.0.1:45837/health
```

## 群聊使用

直接用自然语言告诉机器人你想做什么：

```text
@宋老师管家 今天安排写代码、开会、看文档
@宋老师管家 十分钟后提醒我看电影
@宋老师管家 写一份关于项目进展的飞书文档
@宋老师管家 复盘一下
```

机器人会自动识别意图：
- 包含"安排"、"提醒"、"闹钟" → 整理计划草稿
- 包含"写文档"、"飞书文档" → 创建云文档
- 包含"复盘"、"完成了"、"没做" → 记录完成情况

草稿生成后，回复 **确认** 即可写入你自己的飞书日历。首次使用需要完成 OAuth 授权，授权后自动跳转回飞书聊天窗口。

保留的命令：

- `/help`：查看功能介绍
- `/status`：查看今天的计划
- `/clear`：清空今天的本地计划

## 验证

```bash
uv run ruff format .
uv run pytest
```

本地状态保存在 `.data/state.json`，权限为 `0600`。该文件含各用户 OAuth token，生产部署应使用加密磁盘或替换为带字段加密的数据库。



# 临时取消代理运行
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
uv run song-agent
