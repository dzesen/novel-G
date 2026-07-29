"""图像生成 Provider 的异步任务抽象与协议适配器。"""

from backend.services.image.contracts import (
    ImageCancelResult,
    ImageFailure,
    ImageFailureCode,
    ImageGenerationRequest,
    ImageJobHandle,
    ImagePollResult,
    ImageProvider,
    ImageProviderError,
)

__all__ = [
    "ImageCancelResult",
    "ImageFailure",
    "ImageFailureCode",
    "ImageGenerationRequest",
    "ImageJobHandle",
    "ImagePollResult",
    "ImageProvider",
    "ImageProviderError",
]
