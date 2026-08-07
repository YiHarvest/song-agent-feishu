from __future__ import annotations

import asyncio
import io
import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from song_agent.api.agent_auth import ApiCredential
from song_agent.app import create_app
from song_agent.application.dispatcher import (
    ApplicationDispatcher,
    _resolve_document_context,
    _resolve_reference_context,
)
from song_agent.attachments.cleanup import AttachmentCleanup
from song_agent.attachments.models import (
    AnalyzeImageInput,
    AttachmentAccess,
    ParseDocumentInput,
    TranscribeAudioInput,
)
from song_agent.attachments.repository import AttachmentRepository
from song_agent.attachments.service import AttachmentService
from song_agent.attachments.storage import UnsafeAttachmentError, inspect_file
from song_agent.attachments.tools import AttachmentTools
from song_agent.config import Settings
from song_agent.domain.intents import ExtractedIntent, UserRequest
from song_agent.domain.results import ApplicationResult
from song_agent.feishu.media import FeishuMediaDownloader, FeishuMediaPermissionError
from song_agent.feishu.transport import parse_message_attachments, parse_message_text
from song_agent.media.asr_client import AsrClient
from song_agent.media.vision_client import (
    VisionBusyError,
    VisionClient,
    VisionTimeoutError,
    _remove_dangling_clause,
    _safe_json,
)
from song_agent.models import FeishuIdentity, IncomingAttachmentRef, IncomingMessage
from song_agent.parsers.document_client import MinerUDocumentClient
from song_agent.parsers.local_text_parser import parse_local_text
from song_agent.services.encryption import AesGcmTokenCipher
from song_agent.store import SqliteStore


def attachment_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "feishu_app_id": "app",
        "feishu_app_secret": "secret",
        "llm_base_url": "https://llm.example/v1",
        "llm_api_key": "llm-key",
        "llm_model": "llm",
        "song_agent_attachments_enabled": True,
        "song_agent_attachment_dir": tmp_path / "attachments",
        "song_agent_attachment_temp_dir": tmp_path / "tmp",
        "song_agent_vision_enabled": True,
        "song_agent_vision_api_key": "vision-secret",
        "song_agent_asr_enabled": True,
        "song_agent_document_parser_enabled": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


async def make_store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(
        tmp_path / "attachments.db",
        app_id="app",
        token_cipher=AesGcmTokenCipher({1: b"x" * 32}, 1),
    )
    await store.initialize()
    return store


class FakeVision:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    async def analyze(self, path, media_type, instruction):
        del path, media_type
        self.instructions.append(instruction)
        return {
            "image_type": "screenshot",
            "description": "错误截图",
            "visible_text": ["Traceback"],
            "analysis": "检查调用栈",
            "confidence": 0.9,
        }


class FakeAsr:
    def __init__(self, text: str = "十分钟后提醒我喝水") -> None:
        self.text = text

    async def transcribe(self, path, *, filename, media_type, language):
        del path, filename, media_type
        return {"text": self.text, "language": language, "task_id": "task-1"}


class FakeDocuments:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    async def parse(self, *, attachment_id, local_path):
        del attachment_id
        self.paths.append(local_path)
        return "# 报告\n\n## 结论\n有效。", {"provider": "mineru_vl"}


class FakeDownloader:
    def __init__(self, payload: bytes, filename: str = "") -> None:
        self.payload = payload
        self.filename = filename
        self.calls: list[dict[str, Any]] = []

    async def download(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.payload) > kwargs["max_bytes"]:
            raise ValueError("附件超过大小限制")
        await asyncio.to_thread(kwargs["destination"].write_bytes, self.payload)
        return self.filename


def message(reference: IncomingAttachmentRef, text: str = "") -> IncomingMessage:
    return IncomingMessage(
        message_id="message-1",
        event_id="event-1",
        tenant_key="tenant",
        app_id="app",
        user_id="user",
        open_id="open",
        chat_id="chat",
        chat_type="p2p",
        message_type=reference.kind,
        text=text,
        attachments=[reference],
    )


async def components(tmp_path: Path, payload: bytes, filename: str = ""):
    settings = attachment_settings(tmp_path)
    store = await make_store(tmp_path)
    repository = AttachmentRepository(store)
    vision = FakeVision()
    documents = FakeDocuments()
    tools = AttachmentTools(
        settings,
        store,
        repository,
        vision,  # type: ignore[arg-type]
        FakeAsr(),  # type: ignore[arg-type]
        documents,  # type: ignore[arg-type]
    )
    service = AttachmentService(
        settings,
        FakeDownloader(payload, filename),  # type: ignore[arg-type]
        repository,
        tools,
    )
    await service.initialize()
    return settings, store, repository, vision, documents, tools, service


def test_feishu_attachment_parser_preserves_text_and_extracts_media() -> None:
    post = json.dumps(
        {"content": [[{"tag": "text", "text": "看看"}, {"tag": "img", "image_key": "img-1"}]]}
    )
    assert parse_message_text("post", post) == "看看"
    assert parse_message_attachments("post", post)[0].resource_key == "img-1"
    assert parse_message_attachments("image", '{"image_key":"img-2"}')[0].kind == "image"
    assert parse_message_attachments("audio", '{"file_key":"file-1"}')[0].kind == "audio"
    assert (
        parse_message_attachments(
            "file",
            '{"file_key":"file-2","file_name":"report.pdf"}',
        )[0].kind
        == "document"
    )


def test_tool_inputs_forbid_paths_urls_and_resource_keys() -> None:
    with pytest.raises(ValidationError):
        AnalyzeImageInput.model_validate({"attachment_id": "att_" + "a" * 32, "path": "/etc/passwd"})
    with pytest.raises(ValidationError):
        ParseDocumentInput.model_validate(
            {"attachment_id": "att_" + "a" * 32, "url": "file:///etc/passwd"}
        )
    with pytest.raises(ValidationError):
        TranscribeAudioInput.model_validate(
            {"attachment_id": "att_" + "a" * 32, "file_key": "file-secret"}
        )


def test_magic_validation_blocks_mime_spoofing(tmp_path: Path) -> None:
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"MZ\x00binary")
    with pytest.raises(UnsafeAttachmentError):
        inspect_file(fake, declared_kind="document", filename="fake.pdf")
    fake.write_text("plain text", encoding="utf-8")
    with pytest.raises(UnsafeAttachmentError):
        inspect_file(fake, declared_kind="document", filename="fake.pdf")


def test_local_text_parser_handles_json_and_rejects_binary(tmp_path: Path) -> None:
    source = tmp_path / "data.json"
    source.write_text('{"name":"宋管家"}', encoding="utf-8-sig")
    content, metadata = parse_local_text(source, "application/json", max_chars=1000)
    assert "宋管家" in content
    assert metadata["json_type"] == "dict"
    source.write_bytes(b"text\x00binary")
    with pytest.raises(ValueError):
        parse_local_text(source, "text/plain", max_chars=1000)


@pytest.mark.asyncio
async def test_image_download_creates_scoped_attachment_and_passes_question(tmp_path: Path) -> None:
    _, store, repository, vision, _, _, service = await components(
        tmp_path,
        b"\x89PNG\r\n\x1a\nimage",
    )
    try:
        prepared = await service.prepare(
            message(
                IncomingAttachmentRef(
                    kind="image",
                    resource_key="img-secret",
                    resource_type="image",
                    filename="error.png",
                ),
                "这个报错怎么解决？",
            ),
            "这个报错怎么解决？",
        )
        item = prepared.context["retrieved"][0]
        assert item["attachment_id"].startswith("att_")
        assert prepared.context["retrieved_context"] == prepared.context["retrieved"]
        assert "这个报错怎么解决" in vision.instructions[0]
        assert await repository.get_owned(
            item["attachment_id"],
            AttachmentAccess(tenant_key="tenant", app_id="app", principal_id="user"),
        )
        assert "img-secret" not in json.dumps(prepared.model_dump(), ensure_ascii=False)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_image_without_text_uses_default_instruction(tmp_path: Path) -> None:
    _, store, _, vision, _, _, service = await components(
        tmp_path,
        b"\x89PNG\r\n\x1a\nimage",
    )
    try:
        prepared = await service.prepare(
            message(
                IncomingAttachmentRef(
                    kind="image",
                    resource_key="img",
                    resource_type="image",
                    filename="image.png",
                )
            ),
            "",
        )
        assert "描述图片内容" in vision.instructions[0]
        assert prepared.direct_response == "检查调用栈"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_audio_transcript_is_exact_router_text(tmp_path: Path) -> None:
    _, store, _, _, _, _, service = await components(tmp_path, b"OggS\x00audio")
    try:
        prepared = await service.prepare(
            message(
                IncomingAttachmentRef(
                    kind="audio",
                    resource_key="file",
                    resource_type="file",
                    filename="voice.ogg",
                )
            ),
            "",
        )
        assert prepared.text == "十分钟后提醒我喝水"
        assert prepared.context["retrieved"][0]["asr_task_id"] == "task-1"
        assert prepared.context["retrieved"][0]["transcript"] == prepared.text
        assert prepared.direct_response is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_audio_confirmation_returns_transcript_without_second_llm(tmp_path: Path) -> None:
    _, store, _, _, _, tools, service = await components(tmp_path, b"OggS\x00audio")
    tools.asr = FakeAsr("我现在说话你能听见吗？")  # type: ignore[assignment]
    try:
        prepared = await service.prepare(
            message(
                IncomingAttachmentRef(
                    kind="audio",
                    resource_key="file",
                    resource_type="file",
                    filename="voice.ogg",
                )
            ),
            "",
        )
        assert prepared.direct_response == "听到了。你说的是：**我现在说话你能听见吗？**"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_local_document_keeps_full_content_in_tool_results_only(tmp_path: Path) -> None:
    content = ("# 标题\n" + "正文" * 2000).encode()
    settings, store, _, _, _, _, service = await components(tmp_path, content)
    settings.song_agent_document_max_preview_chars = 100
    try:
        prepared = await service.prepare(
            message(
                IncomingAttachmentRef(
                    kind="document",
                    resource_key="file",
                    resource_type="file",
                    filename="notes.md",
                ),
                "总结",
            ),
            "总结",
        )
        result = prepared.context["retrieved"][0]
        assert result["truncated"] is True
        assert len(result["content_preview"]) == 100
        stored = await store.get_tool_result(
            result["result_ref"],
            tenant_key="tenant",
            app_id="app",
            principal_id="user",
        )
        assert stored is not None
        assert len(stored["payload"]["content"]) > 100
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_pdf_and_docx_use_document_client(tmp_path: Path) -> None:
    pdf = b"%PDF-1.7\nbody"
    _, store, _, _, documents, _, service = await components(tmp_path, pdf)
    try:
        await service.prepare(
            message(
                IncomingAttachmentRef(
                    kind="document",
                    resource_key="pdf",
                    resource_type="file",
                    filename="report.pdf",
                )
            ),
            "",
        )
        assert documents.paths
    finally:
        await store.close()

    docx_buffer = io.BytesIO()
    with zipfile.ZipFile(docx_buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "x")
        archive.writestr("word/document.xml", "x")
    _, store, _, _, documents, _, service = await components(
        tmp_path / "docx",
        docx_buffer.getvalue(),
    )
    try:
        await service.prepare(
            message(
                IncomingAttachmentRef(
                    kind="document",
                    resource_key="docx",
                    resource_type="file",
                    filename="report.docx",
                )
            ),
            "",
        )
        assert documents.paths
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cross_user_and_cross_tenant_attachment_access_is_denied(tmp_path: Path) -> None:
    _, store, _, _, _, tools, service = await components(
        tmp_path,
        b"\x89PNG\r\n\x1a\nimage",
    )
    try:
        prepared = await service.prepare(
            message(
                IncomingAttachmentRef(
                    kind="image",
                    resource_key="img",
                    resource_type="image",
                    filename="image.png",
                )
            ),
            "",
        )
        attachment_id = prepared.context["retrieved"][0]["attachment_id"]
        for access in (
            AttachmentAccess(tenant_key="tenant", app_id="app", principal_id="other"),
            AttachmentAccess(tenant_key="other", app_id="app", principal_id="user"),
        ):
            with pytest.raises(ValueError):
                await tools.analyze_image(
                    access,
                    AnalyzeImageInput(attachment_id=attachment_id),
                )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_attachment_limits_and_unsupported_files_fail_safely(tmp_path: Path) -> None:
    settings, store, _, _, _, _, service = await components(tmp_path, b"unknown")
    settings.song_agent_attachment_max_files_per_message = 1
    reference = IncomingAttachmentRef(
        kind="unknown",
        resource_key="file",
        resource_type="file",
        filename="archive.exe",
    )
    try:
        with pytest.raises(UnsafeAttachmentError):
            await service.prepare(message(reference), "")
        duplicate = message(reference).model_copy(update={"attachments": [reference, reference]})
        with pytest.raises(ValueError):
            await service.prepare(duplicate, "")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_asr_client_uses_file_multipart_and_language_query(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = request.url.query.decode()
        seen["body"] = await request.aread()
        return httpx.Response(
            200,
            json={"text": "你好", "language": "auto", "task_id": "task"},
        )

    settings = attachment_settings(tmp_path)
    client = AsrClient(settings)
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://asr.test",
    )
    source = tmp_path / "voice.ogg"
    source.write_bytes(b"OggS")
    try:
        result = await client.transcribe(
            source,
            filename="voice.ogg",
            media_type="audio/ogg",
            language="auto",
        )
        assert seen["query"] == "language=auto"
        assert b'name="file"' in seen["body"]
        assert result["task_id"] == "task"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_vision_client_uses_configured_model_and_data_url(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class Completions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"image_type":"photo","description":"猫",'
                            '"visible_text":[],"analysis":"猫","confidence":0.8}'
                        )
                    )
                ]
            )

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    settings = attachment_settings(tmp_path, song_agent_vision_model="configured-kimi")
    client = VisionClient(settings)
    real_client = client.client
    assert real_client is not None
    assert real_client._client._trust_env is False
    client.client = fake_client  # type: ignore[assignment]
    source = tmp_path / "image.png"
    source.write_bytes(b"\x89PNG")
    try:
        result = await client.analyze(source, "image/png", "描述")
        assert captured["model"] == "configured-kimi"
        assert captured["messages"][1]["content"][1]["image_url"]["url"].startswith(
            "data:image/png;base64,"
        )
        assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
        assert captured["max_tokens"] == settings.song_agent_vision_max_tokens
        assert result["description"] == "猫"
    finally:
        if real_client is not None:
            await real_client.close()


def test_vision_invalid_json_falls_back_without_secret() -> None:
    result = _safe_json("无法结构化，但图片中有一个错误")
    assert result["image_type"] == "other"
    assert "错误" in result["analysis"]


def test_vision_removes_only_dangling_clause_after_complete_sentence() -> None:
    text = "流程整体合理，包含人工复核。需注意图中"

    assert _remove_dangling_clause(text) == "流程整体合理，包含人工复核。"
    assert _remove_dangling_clause("建议增加异常升级机制") == "建议增加异常升级机制"


@pytest.mark.asyncio
async def test_vision_timeout_is_translated_without_retrying(tmp_path: Path) -> None:
    calls = 0

    class Completions:
        async def create(self, **kwargs):
            nonlocal calls
            del kwargs
            calls += 1
            from openai import APITimeoutError

            raise APITimeoutError(request=httpx.Request("POST", "https://vision.example"))

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    settings = attachment_settings(
        tmp_path,
        song_agent_vision_max_retries=0,
    )
    client = VisionClient(settings)
    real_client = client.client
    client.client = fake_client  # type: ignore[assignment]
    source = tmp_path / "image.png"
    source.write_bytes(b"\x89PNG")
    try:
        with pytest.raises(VisionTimeoutError):
            await client.analyze(source, "image/png", "描述")
        assert calls == 1
    finally:
        if real_client is not None:
            await real_client.close()


@pytest.mark.asyncio
async def test_vision_overload_is_translated_without_retrying(tmp_path: Path) -> None:
    calls = 0

    class Completions:
        async def create(self, **kwargs):
            nonlocal calls
            del kwargs
            calls += 1
            from openai import RateLimitError

            request = httpx.Request("POST", "https://vision.example")
            response = httpx.Response(429, request=request)
            raise RateLimitError(
                "engine overloaded",
                response=response,
                body={"error": {"type": "engine_overloaded_error"}},
            )

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    settings = attachment_settings(tmp_path, song_agent_vision_max_retries=0)
    client = VisionClient(settings)
    real_client = client.client
    client.client = fake_client  # type: ignore[assignment]
    source = tmp_path / "image.png"
    source.write_bytes(b"\x89PNG")
    try:
        with pytest.raises(VisionBusyError):
            await client.analyze(source, "image/png", "描述")
        assert calls == 1
    finally:
        if real_client is not None:
            await real_client.close()


@pytest.mark.asyncio
async def test_mineru_client_uses_company_vl_two_step_extract(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, Any] = {}

    class FakeImage:
        def __init__(self, page_index: int) -> None:
            self.page_index = page_index
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FakeMinerU:
        def two_step_extract(self, image):
            calls.setdefault("pages", []).append(image.page_index)
            return [{"content": f"# 第 {image.page_index + 1} 页"}]

    def build_client(**kwargs):
        calls["client"] = kwargs
        return FakeMinerU()

    def render_batch(path, page_indexes, scale):
        del path
        calls.setdefault("batches", []).append((page_indexes, scale))
        return [FakeImage(index) for index in page_indexes]

    monkeypatch.setattr("song_agent.parsers.document_client.MinerUClient", build_client)
    monkeypatch.setattr(
        "song_agent.parsers.document_client._pdf_page_count",
        lambda path: 3,
    )
    monkeypatch.setattr(
        "song_agent.parsers.document_client._render_pdf_batch",
        render_batch,
    )
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF")
    settings = attachment_settings(
        tmp_path,
        song_agent_mineru_vl_model_name="",
        song_agent_mineru_vl_server_headers='{"X-Company":"song"}',
        song_agent_mineru_vl_page_concurrency=2,
    )
    client = MinerUDocumentClient(settings)

    async def discover(headers):
        calls["discovery_headers"] = headers
        return "mineru"

    client._discover_model = discover  # type: ignore[method-assign]
    markdown, metadata = await client.parse(attachment_id="att_" + "a" * 32, local_path=source)
    assert calls["discovery_headers"] == {"X-Company": "song"}
    assert calls["client"]["backend"] == "http-client"
    assert calls["client"]["model_name"] == "mineru"
    assert calls["client"]["server_url"] == "http://183.147.142.111:63359/v1"
    assert calls["client"]["max_concurrency"] == 4
    assert calls["batches"] == [([0, 1], 2.0), ([2], 2.0)]
    assert sorted(calls["pages"]) == [0, 1, 2]
    assert "<!-- page:1 -->" in markdown
    assert "# 第 3 页" in markdown
    assert metadata == {"provider": "mineru_vl", "model": "mineru", "page_count": 3}
    await client.close()


@pytest.mark.asyncio
async def test_invalid_mineru_headers_disable_only_document_parser(tmp_path: Path) -> None:
    settings = attachment_settings(
        tmp_path,
        song_agent_mineru_vl_server_headers="{invalid",
    )
    client = MinerUDocumentClient(settings)
    with pytest.raises(RuntimeError, match="MinerU VL 初始化失败"):
        await client._get_client()
    assert client._unavailable_reason


@pytest.mark.asyncio
async def test_mineru_page_limit_is_enforced(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF")
    settings = attachment_settings(
        tmp_path,
        song_agent_mineru_vl_model_name="mineru",
        song_agent_mineru_vl_max_pages=2,
    )
    client = MinerUDocumentClient(settings)
    client._client = SimpleNamespace()  # type: ignore[assignment]
    monkeypatch.setattr(
        "song_agent.parsers.document_client._pdf_page_count",
        lambda path: 3,
    )
    try:
        with pytest.raises(RuntimeError, match="超过限制"):
            await client.parse(attachment_id="att_" + "a" * 32, local_path=source)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_feishu_downloader_streams_and_cleans_oversize(tmp_path: Path) -> None:
    response = SimpleNamespace(
        success=lambda: True,
        file=io.BytesIO(b"12345"),
        file_name="a.txt",
        code=0,
        msg="",
    )
    resource = SimpleNamespace(get=lambda request: response)
    client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message_resource=resource))
    )
    downloader = FeishuMediaDownloader(client, timeout_seconds=5)
    destination = tmp_path / "download"
    assert (
        await downloader.download(
            message_id="m",
            resource_key="k",
            destination=destination,
            resource_type="file",
            max_bytes=10,
        )
        == "a.txt"
    )
    assert destination.read_bytes() == b"12345"
    response.file = io.BytesIO(b"12345")
    oversize = tmp_path / "oversize"
    with pytest.raises(ValueError):
        await downloader.download(
            message_id="m",
            resource_key="k",
            destination=oversize,
            resource_type="file",
            max_bytes=2,
        )
    assert not oversize.exists()


@pytest.mark.asyncio
async def test_feishu_downloader_redacts_permission_error(tmp_path: Path) -> None:
    response = SimpleNamespace(
        success=lambda: False,
        file=None,
        file_name="",
        code=99991672,
        msg="Access denied with verbose authorization URL",
    )
    resource = SimpleNamespace(get=lambda request: response)
    client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message_resource=resource)))
    downloader = FeishuMediaDownloader(client, timeout_seconds=5)
    with pytest.raises(
        FeishuMediaPermissionError,
        match="im:message:readonly",
    ) as caught:
        await downloader.download(
            message_id="m",
            resource_key="k",
            destination=tmp_path / "download",
            resource_type="image",
            max_bytes=10,
        )
    assert "authorization URL" not in str(caught.value)


@pytest.mark.asyncio
async def test_cleanup_never_deletes_outside_root(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    repository = AttachmentRepository(store)
    access = AttachmentAccess(tenant_key="tenant", app_id="app", principal_id="user")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    attachment = await repository.create(
        access=access,
        source_message_id="m",
        source_resource_key="k",
        kind="document",
        filename="a.txt",
        storage_path=outside,
        ttl_seconds=1,
    )
    await store.db.execute(
        "UPDATE attachments SET expires_at = ? WHERE attachment_id = ?",
        (int(time.time()) - 1, attachment.attachment_id),
    )
    await store.db.commit()
    try:
        await AttachmentCleanup(repository, tmp_path / "attachments").run_once()
        assert outside.exists()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_attachment_service_recovers_interrupted_rows_on_startup(
    tmp_path: Path,
) -> None:
    settings, store, repository, _, _, _, service = await components(
        tmp_path,
        b"\x89PNG\r\n\x1a\nimage",
    )
    access = AttachmentAccess(tenant_key="tenant", app_id="app", principal_id="user")
    interrupted = await repository.create(
        access=access,
        source_message_id="message-interrupted",
        source_resource_key="image-key",
        kind="image",
        filename="image.png",
        storage_path=settings.song_agent_attachment_dir / "interrupted.png",
        ttl_seconds=3600,
    )
    try:
        await service.initialize()
        recovered = await repository.get_owned(interrupted.attachment_id, access)
        assert recovered is not None
        assert recovered.status == "failed"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_dispatcher_preserves_attachment_retrieved_context() -> None:
    captured: list[UserRequest] = []

    class Extractor:
        async def extract(self, request, business):
            return ExtractedIntent(intent="conversation.general", confidence=1)

    class Conversations:
        async def record_user(self, request):
            pass

        async def record_assistant(self, request, content):
            pass

    class Business:
        async def build_for_intent_extraction(self, request):
            return object()

    class AgentInputs:
        def build_metadata(self, business):
            return {"business": "kept", "retrieved_context": []}

    class General:
        async def run(self, request):
            captured.append(request)
            return ApplicationResult(status="ok", message="ok")

    dispatcher = ApplicationDispatcher(
        Extractor(),  # type: ignore[arg-type]
        General(),  # type: ignore[arg-type]
        Business(),  # type: ignore[arg-type]
        Conversations(),  # type: ignore[arg-type]
        AgentInputs(),  # type: ignore[arg-type]
    )
    await dispatcher.dispatch(
        UserRequest(
            identity=FeishuIdentity(app_id="app", open_id="user"),
            text="分析附件",
            context={"retrieved": [{"attachment_id": "att_" + "a" * 32}]},
        )
    )
    assert captured[0].context["retrieved"][0]["attachment_id"].startswith("att_")
    assert captured[0].context["retrieved_context"] == captured[0].context["retrieved"]
    assert captured[0].context["business"] == "kept"


def test_document_reference_skips_failed_and_repeated_commands() -> None:
    current = UserRequest(
        identity=FeishuIdentity(app_id="app", open_id="user"),
        text="把这句话写入到 hermes 群中的每日记录云文档中",
    )
    business = SimpleNamespace(
        recent_messages=[
            SimpleNamespace(role="assistant", content="需要记录的原句。"),
            SimpleNamespace(role="user", content=current.text),
            SimpleNamespace(role="assistant", content="还需要：text_to_append"),
            SimpleNamespace(role="user", content=current.text),
        ]
    )

    assert _resolve_reference_context(current, business) == {
        "role": "assistant",
        "content": "需要记录的原句。",
    }
    assert _resolve_document_context(
        current,
        {"role": "assistant", "content": "需要记录的原句。"},
    ) == {
        "action": "create",
        "title": None,
        "target_title": None,
        "markdown": "需要记录的原句。",
    }


@pytest.mark.asyncio
async def test_capabilities_keep_api_text_only_and_advertise_feishu_media(
    tmp_path: Path,
) -> None:
    settings = attachment_settings(
        tmp_path,
        song_agent_api_enabled=True,
        song_agent_api_key="sk-test",
        song_agent_api_model_id="song-agent-test",
    )
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/v1/capabilities",
            headers={"Authorization": "Bearer sk-test"},
        )
    body = response.json()
    assert body["input_types"] == ["text"]
    assert body["features"]["api"]["images"] is False
    assert body["features"]["feishu"]["images"] is True
    assert body["features"]["feishu"]["audio"] is True
    assert body["features"]["feishu"]["files"] is True


def test_api_credential_type_remains_unchanged() -> None:
    assert ApiCredential("mentor", "tenant", "principal").principal_id == "principal"
