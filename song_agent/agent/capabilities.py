"""能力名与工具名映射、搜索词表的单一权威来源。

能力名（Capability）与工具名（Tool）解耦：
- Capability 是 Agent 步骤级的最小能力集合，由 `_infer_capabilities` 推断。
- Tool 是 LLM 可见工具的注册名，由 `ToolRegistry` 管理。
- 本模块是两者之间的唯一映射来源，禁止在别处重复维护词表。
"""

from __future__ import annotations

# Capability -> LLM 可见工具名。
# 注意：`tool_results.read` 不在此处默认暴露；只有观察中出现
# `result_ref` 或已执行持久化工具时，由 Runtime 显式加入 `tool_result` 能力。
CAPABILITY_TOOL_NAMES: dict[str, frozenset[str]] = {
    "search": frozenset({"websearch.search"}),
    "image": frozenset({"attachments.analyze_image"}),
    "audio": frozenset({"attachments.transcribe_audio"}),
    "document_parse": frozenset({"attachments.parse_document"}),
    "document_write": frozenset(
        {
            "documents.prepare_create",
            "documents.prepare_append",
        }
    ),
    "plan": frozenset({"plans.save_draft"}),
    "review": frozenset({"reviews.save"}),
    "tool_result": frozenset({"tool_results.read"}),
    "preferences": frozenset(
        {
            "user_preferences.get",
            "user_preferences.update",
        }
    ),
    "answer": frozenset(),
}

# 工具名 -> 能暴露它的 Capability 集合（CAPABILITY_TOOL_NAMES 的反向索引）。
_TOOL_TO_CAPABILITIES: dict[str, set[str]] = {}
for _capability, _tools in CAPABILITY_TOOL_NAMES.items():
    for _tool in _tools:
        _TOOL_TO_CAPABILITIES.setdefault(_tool, set()).add(_capability)
TOOL_TO_CAPABILITIES: dict[str, frozenset[str]] = {
    tool: frozenset(caps) for tool, caps in _TOOL_TO_CAPABILITIES.items()
}

# namespace 前缀 -> 能力（历史 namespace 匹配的兼容层，精确工具名优先）。
# 例如 plans.get_today 通过 namespace "plans" 匹配 plan/review 能力。
# 未列出的 namespace（如 calendar、task、reminder）不匹配任何能力，
# 确定性业务路由不会把 CRUD 工具暴露给 Agent。
NAMESPACE_TO_CAPABILITIES: dict[str, frozenset[str]] = {
    "plans": frozenset({"plan", "review"}),
    "reviews": frozenset({"review"}),
    "documents": frozenset({"document_write"}),
    "websearch": frozenset({"search", "tool_result"}),
    "tool_results": frozenset({"tool_result"}),
    "attachments": frozenset({"image", "audio", "document_parse"}),
    "user_preferences": frozenset({"preferences"}),
}

# 搜索意图的唯一词表来源。`needs_websearch` 与能力推断共享本常量。
WEBSEARCH_HINTS: frozenset[str] = frozenset(
    {
        # 中文显式搜索意图
        "搜索",
        "查一下",
        "查找",
        "查询",
        "检索",
        # 中文时效性/天气
        "最新",
        "新闻",
        "资讯",
        "天气",
        "气温",
        "下雨",
        "降雨",
        "空气质量",
        # 英文等价
        "search",
        "weather",
        "forecast",
        "recent",
        "latest",
        "news",
    }
)


def needs_websearch(text: str) -> bool:
    """判断用户文本是否包含搜索意图。"""
    lowered = text.lower()
    return any(term in lowered for term in WEBSEARCH_HINTS)
