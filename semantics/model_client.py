"""OpenAI-compatible 云端模型客户端（计划书 Task 4 §10）。

核心约束（计划书 §10.1~10.4）：
- 单次 HTTP 调用，默认 60 秒超时；
- 支持 JSON Schema 和兼容 JSON mode；
- 不记录 API Key（只从环境变量注入）；
- 重试由 Task 6 收件箱 Worker 统一管理，客户端不做内部重试；
- 用户消息作为不可信数据字段传入，模型不配置数据库或发送工具；
- 每次调用携带幂等键和请求追踪号，支持审计。

供应商无关：通过 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 环境变量切换服务。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from logger import get_logger

logger = get_logger(__name__)

# 输出格式示例：发送请求时自动附加到 system 提示词末尾，
# 帮助模型稳定返回符合 Schema 的 JSON（离线评测与线上共用）。
_OUTPUT_FORMAT_EXAMPLE = (
    "\n\n输出格式示例（只返回 JSON，不要输出多余文字或 markdown 代码块）：\n"
    "用户消息：收银机坏了，屏幕不亮，位置在前台，3天内要修好\n"
    "示例输出：\n"
    "{\"intent\": \"ticket.create\", \"confidence\": 0.95, "
    "\"fields\": {\"subject\": \"收银机\", \"location\": \"前台\", "
    "\"problem_description\": \"屏幕不亮\", \"sla\": \"3天\"}, "
    "\"evidence\": [\"收银机坏了\", \"屏幕不亮\"]}\n"
    "用户消息：大家下午好\n"
    "示例输出：\n"
    "{\"intent\": \"chat.ignore\", \"confidence\": 0.98, \"fields\": {}, \"evidence\": []}\n"
)

# 注入标记：用于判断示例是否已附加，避免重复拼接
_EXAMPLE_MARKER = "输出格式示例"


class ModelTimeoutError(TimeoutError):
    """模型调用超时（计划书 §10.3）。"""


class ModelResponseError(ValueError):
    """模型返回非 JSON 或不符合预期结构。"""


class OpenAICompatibleModelClient:
    """OpenAI-compatible Chat Completions 接口的轻量异步客户端。

    使用 httpx AsyncClient 实现单次非重试 HTTP 调用。
    API Key 只从构造函数参数或 ``LLM_API_KEY`` 环境变量读取，
    绝不写入日志、异常消息或任何持久化字段。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        response_format: str | None = None,
        thinking_mode: str | None = None,
    ) -> None:
        # 优先使用显式参数，其次环境变量
        self.base_url = (
            base_url
            or os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        self._api_key = (
            api_key
            or os.environ.get("LLM_API_KEY", "")
        )
        self.model = (
            model
            or os.environ.get("LLM_MODEL", "gpt-4o-mini")
        )
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else float(os.environ.get("LLM_TIMEOUT_SECONDS", "60"))
        )
        configured_format = (
            response_format
            or os.environ.get("LLM_RESPONSE_FORMAT", "auto")
        ).lower()
        if configured_format not in {"auto", "json_schema", "json_object"}:
            raise ValueError(
                "LLM_RESPONSE_FORMAT 必须是 auto、json_schema 或 json_object"
            )
        self.response_format = configured_format
        # 思考模式（2026-08-26）：DeepSeek V4 默认开启思考(effort=high)，思考 token
        # 计入 max_tokens，会把 json_object 正文挤空/挤断（官方已知偶发问题），
        # 且思考模式下 temperature 被忽略。auto=仅对 DeepSeek 域名显式禁用。
        self.thinking_mode = (
            thinking_mode
            or os.environ.get("LLM_THINKING_MODE", "auto")
        ).lower()
        if self.thinking_mode not in {"auto", "enabled", "disabled"}:
            raise ValueError(
                "LLM_THINKING_MODE 必须是 auto、enabled 或 disabled"
            )

    @property
    def is_configured(self) -> bool:
        """是否有有效 API Key。"""
        return bool(self._api_key)

    async def complete_json(
        self,
        *,
        payload: dict[str, Any],
        schema: dict[str, Any],
        idempotency_key: str,
        append_output_example: bool = True,
    ) -> dict[str, Any]:
        """发送 Chat Completion 请求，要求返回符合 JSON Schema 的结构化输出。

        **单次调用，不内部重试**（计划书 §8.1：重试由 Inbox Worker 管理）。

        Args:
            payload: 包含 ``messages`` 列表的请求体。
            schema: JSON Schema，用于 ``response_format`` 强制结构化输出。
            idempotency_key: 幂等键，写入请求头，防重复执行。
            append_output_example: 是否自动在 system 提示词末尾附加
                简单的 JSON 输出示例（默认开启；已有示例时不重复注入）。

        Returns:
            模型返回的 JSON 字典。

        Raises:
            ModelTimeoutError: 请求超时（> timeout_seconds）。
            ModelResponseError: HTTP 错误、非 JSON 响应或解析失败。
            OSError: 底层网络错误。
        """
        import httpx

        url = f"{self.base_url}/chat/completions"
        request_trace_id = uuid.uuid4().hex[:12]

        # 附加 JSON 输出示例（不修改调用方传入的 payload，只改发送副本）
        messages = list(payload.get("messages", []))
        if append_output_example and messages:
            first = messages[0]
            content = first.get("content", "")
            if first.get("role") == "system" and _EXAMPLE_MARKER not in content:
                messages[0] = {**first, "content": content + _OUTPUT_FORMAT_EXAMPLE}

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Idempotency-Key": idempotency_key,
            "X-Request-Trace-Id": request_trace_id,
        }

        resolved_format = self._resolved_response_format()
        response_format_body: dict[str, Any]
        if resolved_format == "json_schema":
            response_format_body = {
                "type": "json_schema",
                "json_schema": {
                    "name": "semantic_intent",
                    "strict": True,
                    "schema": schema,
                },
            }
        else:
            response_format_body = {"type": "json_object"}

        request_body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
            # build 类完整 JSON 含多字段，512 会截断；2048 防长字段截断
            # （思考禁用后预算全部留给正文，按实际生成计费，不虚增成本）
            "max_tokens": 2048,
            "response_format": response_format_body,
        }
        resolved_thinking = self._resolved_thinking()
        if resolved_thinking is not None:
            request_body["thinking"] = {"type": resolved_thinking}

        logger.info(
            "模型请求 trace=%s model=%s format=%s thinking=%s idempotency=%s msg_count=%d",
            request_trace_id,
            self.model,
            resolved_format,
            resolved_thinking or "unset",
            idempotency_key,
            len(request_body["messages"]),
        )

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(url, headers=headers, json=request_body)
        except httpx.TimeoutException as exc:
            logger.warning("模型超时 trace=%s timeout=%ss", request_trace_id, self.timeout_seconds)
            raise ModelTimeoutError(f"模型请求超时 ({self.timeout_seconds}s)") from exc

        # 非 2xx → 非法响应
        if response.status_code >= 400:
            # 不记录 Authorization 头
            body_preview = _redact_secrets(response.text[:200], self._api_key)
            logger.warning(
                "模型 HTTP 错误 trace=%s status=%d body_preview=%s",
                request_trace_id,
                response.status_code,
                body_preview,
            )
            raise ModelResponseError(
                f"模型 HTTP {response.status_code}"
            )

        try:
            data = response.json()
        except Exception as exc:
            raise ModelResponseError("模型响应不是有效 JSON") from exc

        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelResponseError(
                f"模型响应缺少 choices[0].message.content: {str(data)[:200]}"
            ) from exc

        # 容错：DeepSeek json_object 已知偶发返回空 content（官方文档承认的问题，
        # 思考模式挤占 max_tokens 会加剧）。空内容单独报错并带 finish_reason 便于诊断。
        if not (content or "").strip():
            raise ModelResponseError(
                "模型返回空 content"
                f"(finish_reason={_safe_finish_reason(data)})"
                "——DeepSeek json_object 已知偶发问题，等待外层重试"
            )

        # 容错：部分模型返回 markdown 代码块包裹的 JSON，或 JSON 后附加分析文字
        result = _extract_json(content)

        logger.info(
            "模型响应 trace=%s intent=%s confidence=%s",
            request_trace_id,
            result.get("intent", "?"),
            result.get("confidence", "?"),
        )

        return result

    async def complete_vision(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        prompt: str | None = None,
        idempotency_key: str,
    ) -> str:
        """OpenAI 兼容视觉请求：图片 base64 传入，返回文本描述。

        请求格式：messages[].content 为 text + image_url 数组。
        """
        import base64 as _b64
        import httpx

        url = f"{self.base_url}/chat/completions"
        request_trace_id = uuid.uuid4().hex[:12]
        b64 = _b64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{mime_type or 'image/png'};base64,{b64}"

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt or "请描述这张图片的内容。"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Idempotency-Key": idempotency_key,
            "X-Request-Trace-Id": request_trace_id,
        }
        request_body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": 1024,
        }
        logger.info(
            "视觉请求 trace=%s model=%s idempotency=%s img_bytes=%d",
            request_trace_id, self.model, idempotency_key, len(image_bytes),
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(url, headers=headers, json=request_body)
        except httpx.TimeoutException as exc:
            logger.warning("视觉请求超时 trace=%s", request_trace_id)
            raise ModelTimeoutError(f"视觉请求超时 ({self.timeout_seconds}s)") from exc

        if response.status_code >= 400:
            raise ModelResponseError(f"视觉请求 HTTP {response.status_code}")

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelResponseError(f"视觉响应解析失败: {str(data)[:200]}") from exc
        logger.info("视觉响应 trace=%s text_len=%d", request_trace_id, len(content or ""))
        return content or ""

    def _resolved_response_format(self) -> str:
        """解析 auto 模式，不通过试错请求进行运行时降级。"""
        if self.response_format != "auto":
            return self.response_format
        return (
            "json_schema"
            if self.base_url.startswith("https://api.openai.com/")
            else "json_object"
        )

    def _resolved_thinking(self) -> str | None:
        """解析思考模式；None=不发送 thinking 参数（非 DeepSeek 服务不加未知字段）。"""
        if self.thinking_mode == "auto":
            return "disabled" if "deepseek" in self.base_url else None
        return self.thinking_mode


def _extract_json(content: str) -> dict[str, Any]:
    """从模型输出中提取第一个完整 JSON 对象。

    处理以下情况：
    1. 纯 JSON
    2. ```json ... ``` 或 ``` ... ``` 包裹
    3. JSON 后面附加分析文字
    4. markdown 包裹 + JSON 后面附加文字
    """
    text = content.strip()

    # 剥离 markdown 代码块
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        # 找最后一个仅含 ``` 的行，其前的都是 JSON 区域
        last_fence = -1
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip() == "```":
                last_fence = i
                break
        if last_fence >= 0:
            lines = lines[:last_fence]
        text = "\n".join(lines).strip()

    # 尝试直接解析
    try:
        return _require_json_object(json.loads(text))
    except json.JSONDecodeError:
        pass

    # 尝试 raw_decode：提取第一个 JSON 对象
    decoder = json.JSONDecoder()
    try:
        result, _ = decoder.raw_decode(text)
        return _require_json_object(result)
    except json.JSONDecodeError:
        pass

    # 最后尝试：找第一个 { 到最后一个 } 之间的内容
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidate = text[first_brace : last_brace + 1]
        try:
            return _require_json_object(json.loads(candidate))
        except json.JSONDecodeError:
            pass
        try:
            result, _ = decoder.raw_decode(text[first_brace:])
            return _require_json_object(result)
        except json.JSONDecodeError:
            pass

    raise ModelResponseError(f"模型 content 不是有效 JSON: {content[:200]}")


def _require_json_object(value: Any) -> dict[str, Any]:
    """模型结构化输出的根节点必须是 JSON 对象。"""
    if not isinstance(value, dict):
        raise ModelResponseError("模型 content 必须是 JSON 对象")
    return value


def _safe_finish_reason(data: dict[str, Any]) -> str:
    """安全读取 choices[0].finish_reason，缺失时返回 unknown。"""
    try:
        return str(data["choices"][0].get("finish_reason") or "unknown")
    except (KeyError, IndexError, AttributeError, TypeError):
        return "unknown"


def _redact_secrets(text: str, api_key: str) -> str:
    """脱敏供应商错误正文中的当前密钥和 Bearer Token。"""
    redacted = text.replace(api_key, "<redacted>") if api_key else text
    return re.sub(r"(?i)Bearer\s+[^\s,;]+", "Bearer <redacted>", redacted)
