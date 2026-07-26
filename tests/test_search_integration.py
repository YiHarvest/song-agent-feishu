#!/usr/bin/env python3
"""Test search MCP integration."""

import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from song_agent.config import Settings
    from song_agent.search.mcp import SearchMcp
    print("✅ Import successful")

    # Test initialization
    settings = Settings(
        feishu_app_id="test_app",
        feishu_app_secret="test_secret",
        llm_base_url="https://api.example.com/v1",
        llm_api_key="test_key",
        llm_model="test-model",
        ydc_api_key="test_ydc_key",
        tavily_api_key="test_tavily_key",
    )

    search_mcp = SearchMcp(settings)
    print("✅ SearchMcp initialized")
    print(f"   YDC API Key: {search_mcp.settings.ydc_api_key}")
    print(f"   Tavily API Key: {search_mcp.settings.tavily_api_key}")

    # Test SearchResult
    from song_agent.search.mcp import SearchResult
    result = SearchResult(
        title="Test",
        url="https://example.com",
        snippet="Test snippet",
        source="you",
    )
    print(f"✅ SearchResult created: {result.title}")

    print("\n🎉 All tests passed!")

except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)