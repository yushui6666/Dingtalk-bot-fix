"""图片多模态解析（v4.1 Task 4A · 分析层）。

- 读取已归档图片（stored_path），用 OpenAI 兼容视觉模型解析内容；
- 结果写入 message_attachments.vision_result_json；
- 解析失败只记 error，不阻塞业务。
"""

from __future__ import annotations

from typing import Any

from config import (
    VISION_API_KEY,
    VISION_BASE_URL,
    VISION_ENABLED,
    VISION_MODEL,
    VISION_PROMPT,
    VISION_TIMEOUT_SECONDS,
)
from db import Database
from images.archive import ImageArchiveStore
from logger import get_logger

logger = get_logger(__name__)


class VisionAnalyzer:
    """图片内容解析：本地字节 → 视觉模型文本 → 回写 DB。"""

    def __init__(
        self,
        *,
        db: Database,
        store: ImageArchiveStore | None = None,
        client: Any | None = None,
        enabled: bool = VISION_ENABLED,
        prompt: str = VISION_PROMPT,
    ) -> None:
        self._db = db
        self._store = store or ImageArchiveStore()
        self._enabled = enabled
        self._prompt = prompt
        if client is None:
            from semantics.model_client import OpenAICompatibleModelClient

            client = OpenAICompatibleModelClient(
                base_url=VISION_BASE_URL,
                api_key=VISION_API_KEY,
                model=VISION_MODEL,
                timeout_seconds=VISION_TIMEOUT_SECONDS,
            )
        self._client = client

    async def analyze_message(self, message_id: str) -> int:
        """解析某条消息的全部已归档未解析附件，返回成功数。"""
        if not self._enabled:
            return 0
        analyzed = 0
        rows = self._db.list_attachment_rows(message_id)
        for row in rows:
            if row.get("analyzed_status") == "ANALYZED" or not row.get("stored_path"):
                continue
            try:
                text = await self._analyze_one(row)
                # 仅保留最终识别结果：若模型回显 system-reminder/memory 流程包装，视为污染丢弃
                if "system-reminder" in text or "# auto memory" in text or '"type": "message"' in text:
                    raise ValueError("模型返回系统提示泄露，已标记失败")
                self._db.update_attachment_vision(
                    row["id"], result=text, status="ANALYZED",
                )
                analyzed += 1
                logger.info("图片解析完成 msg=%s id=%s text_len=%d",
                            message_id, row["id"], len(text))
            except Exception as exc:
                logger.warning("图片解析失败 msg=%s id=%s err=%s",
                               message_id, row["id"], exc)
                self._db.mark_attachment_analyze_error(row["id"], str(exc))
        return analyzed

    async def _analyze_one(self, row: dict[str, Any]) -> str:
        path = self._store.resolve_relative_path(row["stored_path"])
        data = path.read_bytes()
        mime = row.get("mime_type") or "image/png"
        return await self._client.complete_vision(
            image_bytes=data,
            mime_type=mime,
            prompt=self._prompt,
            idempotency_key=f"vision:{row['id']}",
        )
