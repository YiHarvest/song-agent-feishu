"""
LLM 调用模块。

使用 OpenAI SDK 封装 OpenAI 兼容的 Chat Completions API，
支持细粒度超时、错误分类、重试策略和观测指标。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import TypeVar

import httpx
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from .config import Settings

T = TypeVar("T", bound=BaseModel)


# ============================================================================
# 错误类型定义
# ============================================================================
class LLMError(Exception):
    """LLM 调用基础错误"""
    pass


class LLMTimeoutError(LLMError):
    """LLM 超时错误"""
    pass


class LLMConnectionError(LLMError):
    """LLM 连接错误"""
    pass


class LLMRateLimitError(LLMError):
    """LLM 限流错误"""
    pass


class LLMServerError(LLMError):
    """LLM 服务端错误"""
    pass


class LLMAuthenticationError(LLMError):
    """LLM 认证错误"""
    pass


class LLMInvalidResponseError(LLMError):
    """LLM 响应格式错误"""
    pass


class LLMOutputTruncatedError(LLMInvalidResponseError):
    """LLM 因输出上限停止，内容不完整。"""


class LLMParameterError(LLMError):
    """LLM 参数错误（不应重试）"""
    pass


# ============================================================================
# 观测指标
# ============================================================================
@dataclass
class LLMRequestMetrics:
    """LLM 请求观测指标"""
    run_id: str
    step_index: int
    model: str
    request_id: str | None
    duration_ms: int
    first_token_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    prompt_chars: int
    message_count: int
    tool_schema_count: int
    retry_count: int
    timeout_type: str | None  # 'connect', 'read', 'write', 'pool', None
    result_status: str  # 'success', 'timeout', 'error', 'rate_limit', etc.

    def to_log_dict(self) -> dict:
        """转换为日志字典（不包含敏感信息）"""
        return {
            "run_id": self.run_id,
            "step_index": self.step_index,
            "model": self.model,
            "request_id": self.request_id,
            "duration_ms": self.duration_ms,
            "first_token_ms": self.first_token_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "prompt_chars": self.prompt_chars,
            "message_count": self.message_count,
            "tool_schema_count": self.tool_schema_count,
            "retry_count": self.retry_count,
            "timeout_type": self.timeout_type,
            "result_status": self.result_status,
        }


# ============================================================================
# 可重试的异常类型
# ============================================================================
RETRYABLE_ERRORS = (
    APITimeoutError,
    APIConnectionError,
    RateLimitError,
    InternalServerError,
)


# ============================================================================
# 结构化 LLM 调用器
# ============================================================================
class StructuredLlm:
    """
    结构化 LLM 调用器。

    使用 OpenAI SDK 封装 OpenAI 兼容的 Chat Completions API，
    支持细粒度超时、错误分类、重试策略和观测指标。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger(__name__)

        # 配置 HTTP 超时（细粒度）
        timeout = httpx.Timeout(
            timeout=settings.llm_read_timeout_seconds,
            connect=settings.llm_connect_timeout_seconds,
            read=settings.llm_read_timeout_seconds,
            write=settings.llm_write_timeout_seconds,
            pool=settings.llm_pool_timeout_seconds,
        )

        # 配置连接池
        limits = httpx.Limits(
            max_connections=settings.llm_max_connections,
            max_keepalive_connections=settings.llm_max_keepalive_connections,
            keepalive_expiry=settings.llm_keepalive_expiry_seconds,
        )

        # 创建 OpenAI 客户端
        # 注意：max_retries=0，避免 SDK 自动重试和 Song Agent 自己的重试重复叠加
        self.client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_url,
            max_retries=0,
            timeout=timeout,
            http_client=httpx.AsyncClient(
                timeout=timeout,
                limits=limits,
                trust_env=False,  # 忽略系统代理
            ),
        )

        # 重试计数器（用于观测）
        self._retry_counts: dict[str, int] = {}

    async def generate(
        self,
        schema: type[T],
        system: str,
        user: str,
        *,
        run_id: str = "default",
        step_index: int = 0,
        max_tokens: int | None = None,
        tool_schema_count: int = 0,
    ) -> T:
        """
        生成结构化输出。

        Args:
            schema: 输出模型类型
            system: 系统提示词
            user: 用户输入
            run_id: Agent Run ID（用于观测）
            step_index: 步骤索引（用于观测）
            max_tokens: 最大输出 Token（None 表示使用默认值）
            tool_schema_count: 工具 Schema 数量（用于观测）

        Returns:
            结构化输出对象

        Raises:
            LLMError: LLM 调用失败
        """
        # 使用默认 max_tokens（决策阶段）
        if max_tokens is None:
            max_tokens = self.settings.llm_decision_max_tokens

        started_at = time.perf_counter()
        request_fingerprint = hashlib.sha256(f"{system}|{user}".encode()).hexdigest()[:16]

        self.logger.info(
            "🧠 LLM 开始思考 run_id=%s step=%d model=%s output=%s input_chars=%d "
            "max_tokens=%d fingerprint=%s timeout=%ds",
            run_id,
            step_index,
            self.settings.llm_model,
            schema.__name__,
            len(user),
            max_tokens,
            request_fingerprint,
            self.settings.llm_read_timeout_seconds,
        )

        # 初始化重试计数器
        retry_key = f"{run_id}:{step_index}"
        self._retry_counts[retry_key] = 0

        try:
            # 执行带重试的请求
            result = await self._generate_with_retry(
                schema=schema,
                system=system,
                user=user,
                max_tokens=max_tokens,
                run_id=run_id,
                step_index=step_index,
                request_fingerprint=request_fingerprint,
                tool_schema_count=tool_schema_count,
                started_at=started_at,
            )

            # 记录成功指标
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            metrics = LLMRequestMetrics(
                run_id=run_id,
                step_index=step_index,
                model=self.settings.llm_model,
                request_id=None,  # OpenAI SDK 不直接提供
                duration_ms=duration_ms,
                first_token_ms=None,  # 非流式请求
                input_tokens=None,  # 需要从响应中提取
                output_tokens=None,
                prompt_chars=len(system) + len(user),
                message_count=2,  # system + user
                tool_schema_count=tool_schema_count,
                retry_count=self._retry_counts[retry_key],
                timeout_type=None,
                result_status="success",
            )

            self.logger.info(
                "🧠 LLM 思考完成 run_id=%s step=%d output=%s elapsed_ms=%d retries=%d",
                run_id,
                step_index,
                schema.__name__,
                duration_ms,
                metrics.retry_count,
            )

            return result

        except RetryError as e:
            # 重试耗尽
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            self.logger.error(
                "🧠 LLM 重试耗尽 run_id=%s step=%d elapsed_ms=%d retries=%d",
                run_id,
                step_index,
                duration_ms,
                self._retry_counts[retry_key],
            )
            raise self._classify_error(e.last_attempt.exception()) from e

        except Exception as e:
            # 其他错误
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            self.logger.error(
                "🧠 LLM 调用失败 run_id=%s step=%d elapsed_ms=%d error=%s",
                run_id,
                step_index,
                duration_ms,
                type(e).__name__,
            )
            raise self._classify_error(e) from e

        finally:
            # 清理重试计数器
            self._retry_counts.pop(retry_key, None)

    @retry(
        retry=retry_if_exception_type(RETRYABLE_ERRORS),
        stop=stop_after_attempt(2),  # 第一次调用 + 最多一次重试
        wait=wait_random_exponential(multiplier=1, min=1, max=4),
        reraise=True,
    )
    async def _generate_with_retry(
        self,
        schema: type[T],
        system: str,
        user: str,
        max_tokens: int,
        run_id: str,
        step_index: int,
        request_fingerprint: str,
        tool_schema_count: int,
        started_at: float,
    ) -> T:
        """带重试的生成逻辑"""
        retry_key = f"{run_id}:{step_index}"

        try:
            # 构建请求参数
            request_params = {
                "model": self.settings.llm_model,
                "temperature": 0,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }

            # 根据配置选择结构化输出模式
            # - json_schema: OpenAI Structured Outputs，严格保证 Schema
            # - json_object: 宽松模式，只保证是有效 JSON
            # - none: 禁用，完全依赖提示词
            structured_mode = getattr(self.settings, "llm_structured_output_mode", "json_object")

            if structured_mode == "json_schema":
                # OpenAI Structured Outputs: 强制输出符合 schema 的 JSON
                # 比 json_object 更可靠，能保证字段类型和结构
                schema_dict = schema.model_json_schema()
                json_schema = {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": schema_dict,
                }
                request_params["response_format"] = {
                    "type": "json_schema",
                    "json_schema": json_schema,
                }
            elif structured_mode == "json_object":
                # 宽松模式：只保证输出是有效 JSON
                # 大多数 OpenAI 兼容 API（SiliconFlow、DeepSeek、GLM 等）都支持
                request_params["response_format"] = {"type": "json_object"}
            # none: 不设置 response_format，完全依赖提示词

            # 调用 OpenAI API
            self.logger.debug(
                "🧠 LLM API 调用开始 run_id=%s step=%d model=%s",
                run_id,
                step_index,
                self.settings.llm_model,
            )
            api_started_at = time.perf_counter()
            response = await self.client.chat.completions.create(**request_params)
            api_duration_ms = int((time.perf_counter() - api_started_at) * 1000)
            self.logger.debug(
                "🧠 LLM API 调用完成 run_id=%s step=%d duration_ms=%d",
                run_id,
                step_index,
                api_duration_ms,
            )

            # 提取响应内容。达到 token 上限时，即使 JSON 被服务端闭合，
            # 业务正文仍可能停在半句，不能当作成功结果。
            choice = response.choices[0]
            finish_reason = str(getattr(choice, "finish_reason", "") or "")
            if finish_reason in {"length", "max_tokens"}:
                self.logger.warning(
                    "🧠 LLM 输出被截断 run_id=%s step=%d max_tokens=%d "
                    "finish_reason=%s",
                    run_id,
                    step_index,
                    max_tokens,
                    finish_reason,
                )
                raise LLMOutputTruncatedError(
                    f"模型输出达到上限: finish_reason={finish_reason}"
                )
            raw = choice.message.content
            if not raw or not raw.strip():
                raise LLMInvalidResponseError("模型返回了空内容")

            # 结构化输出模式保证 JSON 语法；Pydantic 校验业务结构
            try:
                result = schema.model_validate(json.loads(raw))
            except (json.JSONDecodeError, ValueError) as e:
                raise LLMInvalidResponseError(f"模型返回内容不是有效的 JSON: {e}") from e

            return result

        except RETRYABLE_ERRORS as e:
            # 增加重试计数
            self._retry_counts[retry_key] = self._retry_counts.get(retry_key, 0) + 1

            # 记录重试（包含具体异常信息）
            self.logger.warning(
                "🧠 LLM 请求失败，准备重试 run_id=%s step=%d attempt=%d fingerprint=%s error=%s: %s",
                run_id,
                step_index,
                self._retry_counts[retry_key],
                request_fingerprint,
                type(e).__name__,
                str(e)[:200],
            )

            # 重新抛出异常，让 tenacity 处理重试
            raise

    def _classify_error(self, error: Exception) -> LLMError:
        """分类错误类型"""
        if isinstance(error, APITimeoutError):
            return LLMTimeoutError(f"LLM 请求超时: {error}")
        elif isinstance(error, APIConnectionError):
            return LLMConnectionError(f"LLM 连接失败: {error}")
        elif isinstance(error, RateLimitError):
            return LLMRateLimitError(f"LLM 限流: {error}")
        elif isinstance(error, InternalServerError):
            return LLMServerError(f"LLM 服务端错误: {error}")
        elif isinstance(error, AuthenticationError):
            return LLMAuthenticationError(f"LLM 认证失败: {error}")
        elif isinstance(error, BadRequestError):
            return LLMParameterError(f"LLM 参数错误: {error}")
        elif isinstance(error, LLMError):
            return error
        else:
            return LLMError(f"LLM 调用失败: {error}")

    async def close(self) -> None:
        """关闭客户端"""
        await self.client.close()


# ============================================================================
# 辅助函数
# ============================================================================
def api_error_message(response: httpx.Response) -> str | None:
    """提取 OpenAI 兼容接口的安全错误摘要，不记录响应中的其他数据。"""
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, str):
        return payload.strip() or None
    if not isinstance(payload, dict):
        return None

    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("msg")
    elif isinstance(error, str):
        message = error
    else:
        message = None
    message = message or payload.get("message") or payload.get("msg") or payload.get("detail")
    if not isinstance(message, str) or not message.strip():
        return None

    code = payload.get("code")
    suffix = f" (code={code})" if isinstance(code, str | int) and str(code) else ""
    return f"{message.strip()}{suffix}"
