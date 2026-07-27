# Song Agent OpenAI 兼容 API

外部 Agent API 使用 `/api/v1` 前缀。不要把 `/v1` 反向代理改写到此路径。

## 配置

所有配置来自 `.env`：

```env
SONG_AGENT_API_ENABLED=true
SONG_AGENT_API_MODEL_ID=song-agent-2.1
SONG_AGENT_API_KEY_NAME=mentor
SONG_AGENT_API_KEY=替换为高强度随机密钥
SONG_AGENT_API_DEFAULT_TENANT=mentor
SONG_AGENT_API_APP_ID=mentor-api
SONG_AGENT_API_RATE_LIMIT_PER_MINUTE=30
SONG_AGENT_API_MAX_MESSAGES=30
SONG_AGENT_API_MAX_MESSAGE_CHARS=20000
SONG_AGENT_API_MAX_TOTAL_CHARS=60000
SONG_AGENT_API_SYNC_TIMEOUT_SECONDS=150
SONG_AGENT_API_IDEMPOTENCY_TTL_SECONDS=86400
SONG_AGENT_API_BINDING_CODE_TTL_SECONDS=600
SONG_AGENT_API_HEALTH_DETAILS_ENABLED=false
```

生成 API Key：

```bash
uv run python -c "import secrets; print('sk-song-' + secrets.token_urlsafe(32))"
```

设置 `SONG_AGENT_API_ENABLED=false` 后重启，可关闭全部 `/api/v1` 路由。API 已启用但 Key
为空时，API 返回 `503 api_not_configured`；飞书 Gateway、Scheduler、Outbox 仍可启动。

## OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://0.0.0.0:45837/api/v1",
    api_key="sk-song-example",
)

response = client.chat.completions.create(
    model="song-agent-2.1",
    messages=[{"role": "user", "content": "你好，请介绍一下自己。"}],
)
print(response.choices[0].message.content)
```

可用 `X-Song-User-Id` 隔离 API 用户，用 `X-Song-Conversation-Id` 隔离会话。写请求可带
`Idempotency-Key`；同 Key、同请求返回首次结果，同 Key、不同请求返回 `409`。

## curl

```bash
curl http://0.0.0.0:45837/api/v1/chat/completions \
  -H 'Authorization: Bearer sk-song-example' \
  -H 'Content-Type: application/json' \
  -H 'X-Song-User-Id: mentor-user' \
  -H 'X-Song-Conversation-Id: mentor-thread-1' \
  -H 'Idempotency-Key: request-20260727-001' \
  -d '{
    "model": "song-agent-2.1",
    "messages": [{"role": "user", "content": "你好，请介绍一下自己。"}]
  }'
```

流式调用：

```bash
curl -N http://0.0.0.0:45837/api/v1/chat/completions \
  -H 'Authorization: Bearer sk-song-example' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "song-agent-2.1",
    "stream": true,
    "stream_options": {"include_usage": true},
    "messages": [{"role": "user", "content": "列出你的能力。"}]
  }'
```

## 飞书渠道绑定

先创建一次性绑定码：

```bash
curl -X POST http://0.0.0.0:45837/api/v1/channel-bindings/feishu/code \
  -H 'Authorization: Bearer sk-song-example' \
  -H 'X-Song-User-Id: mentor-user'
```

随后在飞书中发送返回的精确命令，例如 `绑定 A1B2C3D4E5F6`。其他消息继续走原飞书流程。
API 请求如需使用绑定身份，在 `metadata` 传：

```json
{
  "delivery_channel": "feishu",
  "delivery_binding_id": "binding_xxx"
}
```

## 接口

```text
GET    /api/v1/models
GET    /api/v1/models/{model_id}
POST   /api/v1/chat/completions
GET    /api/v1/capabilities
GET    /api/v1/health
GET    /api/v1/health/details
POST   /api/v1/pending-actions/{action_id}/confirm
POST   /api/v1/pending-actions/{action_id}/cancel
POST   /api/v1/channel-bindings/feishu/code
GET    /api/v1/channel-bindings
DELETE /api/v1/channel-bindings/{binding_id}
```

当前仅支持文本消息。图片、文件、语音、外部 `tool` role、`tools`、`tool_choice`、
`/responses`、`/files`、`/assistants` 尚未实现。
