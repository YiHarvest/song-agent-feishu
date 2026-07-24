from song_agent.feishu.transport import clean_incoming_text
from song_agent.models import DailyRecord, ParsedPlanTask
from song_agent.planner import (
    build_tasks,
    detect_exact_intent,
    detect_intent_heuristically,
    format_plan,
    has_explicit_time_hint,
    is_clear_command,
    is_confirmation,
    is_reminder_status_question,
)
from song_agent.workflow import extract_docx_token


def make_record(user_id: str = "user-a") -> DailyRecord:
    return DailyRecord(
        key=f"chat:{user_id}:2026-07-22",
        date="2026-07-22",
        chat_id="chat",
        user_id=user_id,
        plan_status="draft",
        tasks=build_tasks(
            [
                ParsedPlanTask(title="写方案", priority="A", start_time="10:00", end_time="11:30"),
                ParsedPlanTask(title="对齐需求", priority="B"),
            ]
        ),
        created_at="2026-07-22T00:00:00+00:00",
        updated_at="2026-07-22T00:00:00+00:00",
    )


def test_build_tasks_generates_priority_local_ids() -> None:
    assert [task.id for task in make_record().tasks] == ["A1", "B1"]


def test_confirmation_is_deliberately_narrow() -> None:
    assert is_confirmation("确认")
    assert is_confirmation("确认并创建日程")
    assert not is_confirmation("好的")
    assert not is_confirmation("我确认今天要做三件事")


def test_only_exact_commands_bypass_structured_llm_routing() -> None:
    assert detect_intent_heuristically("/plan 今天写方案") == "plan"
    assert detect_intent_heuristically("/remind 20:30 测试") == "reminder"
    assert detect_intent_heuristically("/doc 创建测试记录") == "document"
    assert detect_intent_heuristically("/review 写方案完成了") == "review"

    # Natural language, including keyword-heavy text, must be classified by the LLM.
    assert detect_intent_heuristically("今天规划") == "unknown"
    assert detect_intent_heuristically("10分钟后提醒我交pr") == "unknown"
    assert detect_intent_heuristically("创建一份飞书云文档") == "unknown"


def test_negative_or_meta_language_never_hits_a_write_route_locally() -> None:
    for text in (
        "这个安排不合理，不要创建日程",
        "别提醒我看电影",
        "复盘功能现在有问题",
        "他说让我创建一份文档，但先不要做",
    ):
        assert detect_exact_intent(text) is None
        assert detect_intent_heuristically(text) == "unknown"


def test_intent_recognizes_greetings_and_capability_questions_as_chat() -> None:
    assert detect_intent_heuristically("你好") == "chat"
    assert detect_intent_heuristically("你能做什么？") == "chat"
    assert detect_intent_heuristically("谢谢！") == "chat"


def test_plan_output_requires_confirmation_and_mentions_own_calendar() -> None:
    output = format_plan(make_record())
    assert "确认卡片" in output
    assert "你自己的飞书日历" in output
    assert "10:00-11:30" in output


def test_natural_clear_and_reminder_status_commands() -> None:
    assert is_clear_command("清空")
    assert is_clear_command("清空计划")
    assert is_clear_command("/clear")
    assert not is_clear_command("清空文档")
    assert is_reminder_status_question("闹钟定了吗")
    assert is_reminder_status_question("提醒设置好了吗？")


def test_time_hint_supports_chinese_and_numeric_relative_time() -> None:
    assert has_explicit_time_hint("10分钟后提醒我交pr")
    assert has_explicit_time_hint("十分钟后提醒我交pr")
    assert has_explicit_time_hint("下午 3 点提醒我")
    assert not has_explicit_time_hint("提醒我交pr")


def test_extract_docx_token_from_feishu_link() -> None:
    assert (
        extract_docx_token("追加到 https://feishu.cn/docx/MsabdjqNDojTUix2I0tcF5NEnbb")
        == "MsabdjqNDojTUix2I0tcF5NEnbb"
    )
    assert extract_docx_token("没有文档链接") is None


def test_invisible_feishu_text_is_ignored() -> None:
    assert clean_incoming_text("\u200b\u200c\ufeff") == ""
    assert clean_incoming_text("@_user_1\u200b 10分钟后提醒我喝水") == "10分钟后提醒我喝水"
