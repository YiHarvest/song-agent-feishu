# 飞书图片、语音和文件解析

该能力只接入飞书消息。外部 `/api/v1/chat/completions` 仍然只接受文本。

飞书应用须订阅 `im.message.receive_v1`，单聊场景开通
`im:message.p2p_msg:readonly`；下载用户消息中的图片、语音和文件还须开通
`im:message:readonly`，否则资源下载会被飞书以 `99991672 Access denied`
拒绝。开通权限后发布应用新版本。

用户发送的图片必须通过消息资源接口
`GET /open-apis/im/v1/messages/:message_id/resources/:file_key?type=image`
下载。`GET /open-apis/im/v1/images/:image_key` 只能下载当前机器人自己上传的图片，
不能用于读取用户发来的图片。

Kimi K2.6 默认开启思考模式，但普通截图理解不需要长推理。图片链路默认设置
`SONG_AGENT_VISION_THINKING_ENABLED=false` 使用即时模式，并将输出限制为 800
token。视觉客户端默认不自动重试长耗时请求，最大等待 45 秒，避免一次超时被 SDK
隐式放大成数分钟。需要深度视觉推理或自动重试时，可显式调整对应配置。

## 公司 MinerU VL

```env
SONG_AGENT_DOCUMENT_PARSER_ENABLED=true
SONG_AGENT_DOCUMENT_PARSER_PROVIDER=mineru_vl
SONG_AGENT_MINERU_VL_BASE_URL=http://183.147.142.111:63359/v1
SONG_AGENT_MINERU_VL_MODEL_NAME=
SONG_AGENT_MINERU_VL_SERVER_HEADERS={}
SONG_AGENT_MINERU_VL_CONNECT_TIMEOUT_SECONDS=10
SONG_AGENT_MINERU_VL_READ_TIMEOUT_SECONDS=300
SONG_AGENT_MINERU_VL_PDF_SCALE=2.0
SONG_AGENT_MINERU_VL_MAX_PAGES=100
SONG_AGENT_MINERU_VL_PAGE_CONCURRENCY=2
SONG_AGENT_MINERU_VL_REGION_CONCURRENCY=4
```

`SONG_AGENT_MINERU_VL_MODEL_NAME` 为空时，首次解析调用 `/v1/models`，缓存首个
模型 ID。PDF 由 `pypdfium2` 分页渲染，再由 `mineru-vl-utils` 的
`two_step_extract` 完成布局、文字、表格和公式识别。每批最多处理两页。

Agent 仍只传 `attachment_id`，不能传本地路径、公司接口 URL、请求头、飞书
file_key 或 image_key。请求头不写日志。无效请求头 JSON 只禁用文档解析，不影响
飞书、日程、提醒和其他附件能力。

## 支持范围

- 图片：PNG、JPEG、GIF、WebP，经 Kimi `kimi-k2.6` 理解。
- 语音：MP3、WAV、OGG、M4A、AMR，经配置的 ASR 服务转写。
- 本地文本：TXT、Markdown、CSV、JSON。
- MinerU VL：PDF。
- DOCX、PPTX、XLSX、HTML：当前不送入 MinerU VL。

附件按租户、应用和用户三层隔离。文件使用随机内部名称保存，过期文件由现有
APScheduler 实例的低优先级任务清理。

## 验证

```bash
uv run pytest -q
uv run ruff check .
```

在飞书中分别发送图片、语音和 PDF。附件消息会先解析，再进入原有
`RequestRouter`；语音转写文本会作为原始用户文本进入意图识别，因此语音提醒仍走
Pending Action 和原调度器流程。
