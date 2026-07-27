from __future__ import annotations

import logging
import time
from pathlib import Path

from .repository import AttachmentRepository
from .storage import ensure_within_root


class AttachmentCleanup:
    def __init__(self, repository: AttachmentRepository, root: Path) -> None:
        self.repository = repository
        self.root = root.expanduser().resolve()
        self.logger = logging.getLogger(__name__)

    async def run_once(self) -> None:
        for attachment in await self.repository.expired_candidates(int(time.time())):
            try:
                path = ensure_within_root(attachment.storage_path, self.root)
                path.unlink(missing_ok=True)
                await self.repository.transition(
                    attachment.attachment_id,
                    from_statuses=(attachment.status,),
                    to_status="expired",
                )
            except Exception:
                self.logger.warning(
                    "附件清理失败 attachment_id=%s",
                    attachment.attachment_id,
                    exc_info=True,
                )
