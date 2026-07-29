"""Decode and identify supported raster images from their actual bytes."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import warnings

from PIL import Image, UnidentifiedImageError


class InvalidImageError(ValueError):
    """Raised when bytes are not a decodable supported raster image."""


@dataclass(frozen=True, slots=True)
class ProbedImage:
    mime: str
    extension: str
    width: int
    height: int


MAX_MANAGED_IMAGE_PIXELS = 64_000_000
MAX_MANAGED_IMAGE_FRAMES = 1_000
MAX_MANAGED_TOTAL_FRAME_PIXELS = 128_000_000
_SUPPORTED_FORMATS = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "GIF": ("image/gif", ".gif"),
    "WEBP": ("image/webp", ".webp"),
}


def _invalid(message: str) -> InvalidImageError:
    return InvalidImageError(message)


def _inspect_header(payload: bytes) -> tuple[ProbedImage, int]:
    with Image.open(BytesIO(payload)) as image:
        format_name = str(image.format or "").upper()
        mapped = _SUPPORTED_FORMATS.get(format_name)
        if mapped is None:
            raise _invalid(
                "image format must be PNG, JPEG, GIF, or WebP"
            )
        width, height = image.size
        if width <= 0 or height <= 0:
            raise _invalid("image width and height must be positive")
        pixels = width * height
        if pixels > MAX_MANAGED_IMAGE_PIXELS:
            raise _invalid(
                "image decode exceeds the managed 64-megapixel limit"
            )
        frame_count = int(getattr(image, "n_frames", 1))
        if frame_count < 1 or frame_count > MAX_MANAGED_IMAGE_FRAMES:
            raise _invalid(
                "image decode exceeds the managed frame-count limit"
            )
        if pixels * frame_count > MAX_MANAGED_TOTAL_FRAME_PIXELS:
            raise _invalid(
                "image decode exceeds the managed total-frame pixel limit"
            )
        mime, extension = mapped
        return (
            ProbedImage(
                mime=mime,
                extension=extension,
                width=width,
                height=height,
            ),
            frame_count,
        )


def probe_image(payload: bytes | bytearray | memoryview) -> ProbedImage:
    """Identify and fully decode an allowlisted image without external hints."""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("image payload must be bytes-like")
    content = bytes(payload)
    if not content:
        raise _invalid("image payload is empty")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            probed, frame_count = _inspect_header(content)

            # verify() checks container integrity without retaining decoded
            # pixels. Reopen and load every frame so a valid-looking header
            # with corrupt compressed data cannot become a managed asset.
            with Image.open(BytesIO(content)) as image:
                image.verify()
            with Image.open(BytesIO(content)) as image:
                if (
                    str(image.format or "").upper()
                    not in _SUPPORTED_FORMATS
                    or image.size != (probed.width, probed.height)
                ):
                    raise _invalid(
                        "decoded image identity changed during verification"
                    )
                if int(getattr(image, "n_frames", 1)) != frame_count:
                    raise _invalid(
                        "decoded image frame count changed during verification"
                    )
                for frame_index in range(frame_count):
                    image.seek(frame_index)
                    image.load()
    except InvalidImageError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise _invalid(
            "image decode failed: invalid or corrupt supported image data"
        ) from exc

    return probed
