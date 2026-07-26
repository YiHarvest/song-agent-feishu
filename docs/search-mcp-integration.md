# Search Engine MCP 集成文档

## 概述

Song Agent 现已集成 `search-engine-tool-mcp`，支持通过 You.com 和 Tavily 搜索引擎进行网络搜索。

## 配置

### 1. 环境变量配置

在 `.env` 文件中添加以下配置：

```bash
# SearXNG（推荐，免费自托管）
SEARXNG_BASE_URL="http://127.0.0.1:8080"

# TalorData（推荐，高质量 SERP API）
TALORDATA_API_KEY="your_talordata_api_key"

# You.com API Key（可选）
YDC_API_KEY="your_ydc_api_key"

# Tavily API Key（可选）
TAVILY_API_KEY="your_tavily_api_key"
```

至少配置一个 API Key 才能使用搜索功能。推荐配置：
- **SearXNG**：免费，隐私友好，自托管
- **TalorData**：高质量 SERP API，支持 AI 答案

### 2. 依赖安装

```bash
uv add search-engine-tool-mcp==0.4.3
```

或手动添加到 `pyproject.toml`：

```toml
dependencies = [
  # ... 其他依赖
  "search-engine-tool-mcp>=0.4.3,<1",
]
```

## 使用方式

### 1. 自然语言搜索

用户可以通过自然语言触发搜索：

```
用户：搜索最新的 AI 新闻
用户：查找 Python 异步编程最佳实践
用户：查询 React 19 新特性
```

### 2. 工具调用

Agent 会自动识别搜索意图并调用 `websearch.search` 工具：

```python
# 工具参数
{
  "query": "搜索查询字符串",
  "provider": "auto",  # "auto", "searxng", "talordata", "you", "tavily"
  "max_results": 5,    # 1-20
  "search_depth": "basic",  # "basic" 或 "advanced"（仅 Tavily）
  "include_answer": False   # 是否包含 AI 答案（仅 Tavily 和 TalorData）
}
```

### 3. 搜索结果格式

搜索结果会以结构化格式返回：

```
搜索结果（auto）：

1. **标题**
   摘要内容
   [链接](https://example.com)

2. **标题2**
   摘要内容2
   [链接](https://example2.com)
```

## 架构设计

### 1. 模块结构

```
song_agent/
├── search/
│   ├── __init__.py
│   └── mcp.py          # SearchMcp 类
├── workflow.py         # 集成搜索工具
└── app.py              # 初始化 SearchMcp
```

### 2. 工具注册

在 `workflow.py` 中注册搜索工具：

```python
(
    "websearch.search",
    "使用搜索引擎搜索信息。用于用户说「搜索」「查找」「查询」等需要联网搜索的请求。",
    self._tool_websearch,
    "local",
    _websearch_arguments_schema(),
)
```

### 3. 能力推断

在 `agent/runtime.py` 中添加搜索关键词识别：

```python
if any(kw in user_text for kw in ["搜索", "查找", "search", "查询", "检索"]):
    capabilities.add("websearch")
```

## API 参考

### SearchMcp 类

```python
class SearchMcp:
    def __init__(self, settings: Settings) -> None:
        """初始化搜索引擎 MCP"""

    async def search(
        self,
        query: str,
        *,
        provider: str = "auto",
        max_results: int = 5,
        search_depth: str = "basic",
        include_answer: bool = False,
    ) -> list[SearchResult]:
        """执行搜索查询"""
```

### SearchResult 数据类

```python
@dataclass
class SearchResult:
    title: str        # 结果标题
    url: str          # 结果链接
    snippet: str      # 结果摘要
    source: str = ""  # 搜索引擎来源
```

## 错误处理

### 1. 未配置 API Key

如果未配置任何 provider，MCP 会返回明确错误：

```python
await search_mcp.search("test query")
# raises SearchMcpError
```

### 2. 搜索失败

搜索失败时会记录错误日志并抛出 `SearchMcpError`，由 Agent Runtime 记录为工具错误：

```python
try:
    results = await search_mcp.search("query")
except SearchMcpError as e:
    logger.error(f"Search failed: {e}")
```

## 测试

运行测试：

```bash
pytest tests/test_search_mcp.py -v
```

## 注意事项

1. **API Key 安全**：不要将 API Key 提交到版本控制系统
2. **搜索频率**：注意 API 调用频率限制
3. **结果质量**：不同搜索引擎返回的结果可能不同
4. **超时处理**：搜索可能需要几秒钟，注意设置合理的超时时间

## 未来改进

1. 支持更多搜索引擎（Google、Bing 等）
2. 添加搜索结果缓存
3. 支持搜索结果去重
4. 添加搜索历史记录
5. 支持图片搜索和新闻搜索
