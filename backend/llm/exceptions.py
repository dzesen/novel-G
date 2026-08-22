"""LLM 模块自定义异常体系。"""


class LLMError(Exception):
    """LLM 调用基础异常。"""

    def __init__(self, message: str = "", provider: str = "", model: str = ""):
        self.provider = provider
        self.model = model
        super().__init__(message)


class LLMAuthError(LLMError):
    """API Key 无效或权限不足。"""


class LLMRateLimitError(LLMError):
    """触发服务商速率限制。"""


class LLMTimeoutError(LLMError):
    """请求超时。"""


class LLMConnectionError(LLMError):
    """Provider 连接建立或传输链路失败。"""


class LLMHTTPStatusError(LLMError):
    """Provider 返回非成功 HTTP 状态，但没有更具体的安全分类。"""

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        provider: str = "",
        model: str = "",
    ) -> None:
        self.status_code = status_code
        super().__init__(message, provider=provider, model=model)


class LLMResponseError(LLMError):
    """响应内容解析失败。"""


class LLMSchemaError(LLMError):
    """结构化输出（Schema）生成或解析失败。"""


class LLMSchemaUnsupportedError(LLMSchemaError):
    """Provider 明确拒绝当前原生 Schema 协议，可安全改用提示词 JSON。"""


class LLMStructuredValidationError(LLMSchemaError):
    """Provider 已返回并计费，但内容未通过本地结构校验。"""

    def __init__(
        self,
        message: str = "",
        *,
        raw_output: str = "",
        provider: str = "",
        model: str = "",
    ) -> None:
        self.raw_output = raw_output
        super().__init__(message, provider=provider, model=model)


def is_schema_protocol_unsupported(exc: BaseException) -> bool:
    """只识别 Provider 对 Schema/工具协议的明确 4xx 拒绝。

    鉴权、限流、超时和普通网络异常不得触发另一笔降级请求。
    """
    parts: list[str] = []
    statuses: set[int] = set()
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.extend((type(current).__name__, str(current)))
        status = getattr(current, "status_code", None)
        if isinstance(status, int):
            statuses.add(status)
        current = current.__cause__ or current.__context__
    if statuses and not statuses.intersection({400, 404, 422}):
        return False
    message = " ".join(parts).lower()
    protocol_marker = any(
        marker in message
        for marker in (
            "response_format",
            "response schema",
            "response_schema",
            "json schema",
            "structured output",
            "tool_choice",
            "tool use",
            "tool_use",
        )
    )
    rejection_marker = any(
        marker in message
        for marker in ("unsupported", "not support", "unknown", "invalid", "not available")
    )
    return protocol_marker and rejection_marker
