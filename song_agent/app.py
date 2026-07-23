"""
FastAPI 应用模块。

创建并配置 FastAPI 应用实例，管理服务生命周期。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import Settings
from .feishu.mcp import FeishuMcp
from .feishu.oauth import FeishuOAuth
from .feishu.transport import FeishuTransport
from .llm import StructuredLlm
from .planner import Planner
from .scheduler import start_scheduler
from .store import JsonStore
from .workflow import AgentWorkflow


def create_app(settings: Settings | None = None) -> FastAPI:
    """
    创建并配置 FastAPI 应用实例。

    Args:
        settings: 可选的配置实例，未提供时从环境变量加载。

    Returns:
        配置完成的 FastAPI 应用实例。
    """
    config = settings or Settings()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    store = JsonStore(config.data_file)
    oauth = FeishuOAuth(config, store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await store.initialize()
        transport = FeishuTransport(config, store)
        planner = Planner(StructuredLlm(config), config.timezone)
        mcp = FeishuMcp(config)
        workflow = AgentWorkflow(planner, store, transport, oauth, mcp)
        oauth.on_authorized = lambda chat_id: transport.send_markdown(
            chat_id,
            "✅ 你的飞书日历与云文档授权已完成。日程请再次回复“确认”；文档请重新发送要求。",
        )
        transport.start(asyncio.get_running_loop(), workflow.enqueue)
        scheduler = start_scheduler(config, store, transport)
        app.state.store = store
        app.state.transport = transport
        app.state.oauth = oauth
        app.state.workflow = workflow
        logging.getLogger(__name__).info("FastAPI 服务已启动: %s", config.base_url)
        yield
        scheduler.shutdown(wait=False)

    app = FastAPI(title="Song Agent", version="0.2.0", lifespan=lifespan)
    app.include_router(oauth.router)

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    return app


app = create_app()
