from song_agent.feishu.mcp import markdown_to_text_blocks
from song_agent.feishu.transport import clean_incoming_text, parse_message_text


def test_plain_text_and_mentions_are_parsed() -> None:
    raw = parse_message_text("text", '{"text":"@_user_1 /help"}')
    assert clean_incoming_text(raw) == "/help"


def test_post_content_is_flattened() -> None:
    raw = parse_message_text(
        "post",
        '{"zh_cn":{"content":[[{"tag":"text","text":"今天"},{"tag":"text","text":"安排"}]]}}',
    )
    assert raw == "今天\n安排"


def test_unsupported_media_is_empty() -> None:
    assert parse_message_text("audio", '{"file_key":"abc"}') == ""


def test_markdown_becomes_safe_docx_text_blocks() -> None:
    blocks = markdown_to_text_blocks("# 测试文档\n\n## 结果\n\n- 通过", "测试文档")
    assert [block["text"]["elements"][0]["text_run"]["content"] for block in blocks] == [
        "结果",
        "• 通过",
    ]
