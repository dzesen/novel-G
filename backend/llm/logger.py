"""LLM 日志：保留调用摘要，对调试详情脱敏并限制输出规模。"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from backend.runtime import is_backend_debug_enabled

if TYPE_CHECKING:
    from backend.llm.models import LLMRequest, LLMResponse

logger = logging.getLogger("llm")
MAX_DEBUG_CHARS = 12_000
MAX_TEXT_CHARS = 4_000
_PRIVATE_KEY = re.compile(
    r"^(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|"
    r"proxy[_-]?authorization|cookie|set[_-]?cookie|password|secret|client[_-]?secret|"
    r"reasoning(?:_content)?|thinking(?:_content)?)$", re.I,
)
_SECRET_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._~+/-]+=*)", re.I)
_SECRET_FIELD = re.compile(
    r"""(["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;&}]+)""",
    re.I,
)
_URL_CREDENTIALS = re.compile(r"(://)[^/\s:@]+:[^/\s@]+@")
_THINK = re.compile(r"<(think|thinking|reasoning)\b[^>]*>.*?(?:</\1\s*>|$)", re.I | re.S)


def _safe_text(value: str, secrets: tuple[str, ...] = (), limit: int = MAX_TEXT_CHARS) -> str:
    text = value
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _THINK.sub("[REDACTED]", text)
    text = _SECRET_TOKEN.sub("[REDACTED]", text)
    text = _SECRET_FIELD.sub(r"\1[REDACTED]", text)
    text = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", text)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    if len(text) > limit:
        return text[:limit] + "…[truncated]"
    return text


def _format_debug_payload(payload: Any, secrets: tuple[str, ...] = ()) -> str:
    # Limit traversal as well as the resulting log line; never stringify an
    # arbitrary SDK object whose repr may expose credentials or be expensive.
    remaining = 128

    def visit(value: Any, depth: int = 0) -> Any:
        nonlocal remaining
        if remaining <= 0 or depth > 6:
            return "[truncated]"
        remaining -= 1
        if isinstance(value, dict):
            result = {}
            for index, (key, child) in enumerate(value.items()):
                if index >= 32 or remaining <= 0:
                    result["[truncated]"] = True
                    break
                name = _safe_text(str(key), secrets, 120)
                result[name] = "[REDACTED]" if _PRIVATE_KEY.fullmatch(str(key)) else visit(child, depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            result = []
            for child in value[:32]:
                if remaining <= 0:
                    break
                result.append(visit(child, depth + 1))
            if len(result) < len(value):
                result.append("[truncated]")
            return result
        if isinstance(value, str):
            return _safe_text(value, secrets)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return "[unsupported value]"

    try:
        text = json.dumps(visit(payload), ensure_ascii=False, indent=2)
    except Exception:
        return "[unavailable payload]"
    return text if len(text) <= MAX_DEBUG_CHARS else text[:MAX_DEBUG_CHARS] + "\n[truncated]"


def log_llm_request(request: LLMRequest, provider: str, *, secrets: tuple[str, ...] = ()) -> None:
    """记录请求摘要；仅显式调试时记录有界详情。"""
    context = {
        key: value for key, value in (request.metadata or {}).items()
        if key in {"request_id", "run_id", "job_id", "chapter_id", "capability_run_id"}
    }
    logger.info(
        "[LLM 请求] provider=%s model=%s messages=%d temperature=%s max_tokens=%s context=%s",
        _safe_text(provider, secrets, 200),
        _safe_text(request.model or "(default)", secrets, 200),
        len(request.messages), request.temperature, request.max_tokens,
        _format_debug_payload(context, secrets),
    )
    if is_backend_debug_enabled() and logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "[LLM 请求详情] provider=%s payload=\n%s",
            _safe_text(provider, secrets, 200),
            _format_debug_payload(request.model_dump(), secrets),
        )


def log_llm_response(response: LLMResponse, *, secrets: tuple[str, ...] = ()) -> None:
    """记录响应摘要，不修改原始响应或用量对象。"""
    provider = _safe_text(response.provider, secrets, 200)
    model = _safe_text(response.model, secrets, 200)
    if response.success:
        logger.info(
            "[LLM 响应] provider=%s model=%s tokens=%d duration=%dms finish=%s",
            provider, model, response.usage.total_tokens, response.duration_ms,
            _safe_text(response.finish_reason, secrets, 200),
        )
    else:
        logger.warning(
            "[LLM 响应失败] provider=%s model=%s error=%s duration=%dms",
            provider, model, _safe_text(response.error, secrets), response.duration_ms,
        )
    if is_backend_debug_enabled() and logger.isEnabledFor(logging.DEBUG):
        payload = response.raw_response if response.raw_response is not None else {"content": response.content}
        logger.debug(
            "[LLM 原始响应] provider=%s model=%s payload=\n%s",
            provider, model, _format_debug_payload(payload, secrets),
        )


def log_provider_test_raw_response(
    provider: str, capability: str, payload: Any, *, secrets: tuple[str, ...] = (),
) -> None:
    """能力测试同样使用有界、脱敏的调试详情。"""
    if not is_backend_debug_enabled() or not logger.isEnabledFor(logging.DEBUG):
        return
    logger.debug(
        "[Provider 测试原始响应] provider=%s capability=%s payload=\n%s",
        _safe_text(provider, secrets, 200), _safe_text(capability, secrets, 200),
        _format_debug_payload(payload, secrets),
    )


def log_llm_error(
    error: Exception, *, provider: str = "", model: str = "", secrets: tuple[str, ...] = (),
) -> None:
    """异常信息也先脱敏，避免错误回显凭据或伪造后续日志行。"""
    logger.error(
        "[LLM 异常] provider=%s model=%s type=%s message=%s",
        _safe_text(provider, secrets, 200), _safe_text(model, secrets, 200),
        type(error).__name__, _safe_text(str(error), secrets),
    )
