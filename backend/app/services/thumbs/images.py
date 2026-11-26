"""通过文件流生成三档 JPEG 缓存，不把原图复制到本地。"""
from __future__ import annotations

import os
import io
import struct
import tempfile
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps, UnidentifiedImageError

VARIANT_SIZES = (300, 1600, 1024)
PREVIEW_MAX_BYTES = 48 * 1024 * 1024


class ImageDecodeError(Exception):
    """可安全向用户展示的图像解码失败原因。"""


def _decode(source: BinaryIO, pixel_limit: int | None = Image.MAX_IMAGE_PIXELS) -> Image.Image:
    try:
        # 预检允许的主图格式只有 JPEG 和 PNG，避免启用无关解码器。
        with Image.open(source, formats=("JPEG", "PNG")) as original:
            original.seek(0)
            if pixel_limit is not None and original.width * original.height > pixel_limit:
                raise ImageDecodeError("图片像素过大，无法生成缩略图")
            original.load()
            with ImageOps.exif_transpose(original) as oriented:
                if "A" in oriented.getbands() or "transparency" in oriented.info:
                    with oriented.convert("RGBA") as rgba, rgba.getchannel("A") as alpha:
                        decoded = Image.new("RGB", rgba.size, "white")
                        decoded.paste(rgba, mask=alpha)
                else:
                    decoded = oriented.convert("RGB")
                # 只保留像素，元数据不进入任何输出档位。
                decoded.info.clear()
                return decoded
    except Image.DecompressionBombError:
        raise ImageDecodeError("图片像素过大，无法生成缩略图") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError, struct.error, OverflowError, MemoryError):
        raise ImageDecodeError("图片损坏或无法解码") from None


def render_preview(source: BinaryIO, size: int) -> bytes:
    with _decode(source, 24_000_000) as image, io.BytesIO() as output:
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        image.save(output, format="JPEG", quality=85)
        return output.getvalue()


def cached_variant(directory: Path, size: int) -> bytes | None:
    """校验本地 JPEG 的规格和解码结果，损坏缓存视作缺失。"""
    try:
        with (directory / f"{size}.jpg").open("rb") as stream:
            limit = size * size * 4 + 65536
            picture = stream.read(limit + 1)
        if len(picture) > limit or not picture.endswith(b"\xff\xd9"):
            return None
        with Image.open(io.BytesIO(picture), formats=("JPEG",)) as image:
            if image.mode != "RGB" or not 0 < min(image.size) <= max(image.size) <= size:
                return None
            image.load()
        return picture
    except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError):
        return None


def render_variants(source: BinaryIO, destination: Path, sizes: tuple[int, ...] = VARIANT_SIZES) -> None:
    """校正方向后保比例缩小；小图不放大，临时文件全部完成才发布。"""
    temporary: list[tuple[Path, Path]] = []
    with _decode(source) as decoded:
        destination.mkdir(parents=True, exist_ok=True)
        try:
            for size in sizes:
                with decoded.copy() as variant:
                    variant.thumbnail((size, size), Image.Resampling.LANCZOS)
                    variant.info.clear()
                    with tempfile.NamedTemporaryFile(prefix=f".{size}-", suffix=".tmp", dir=destination, delete=False) as output:
                        path = Path(output.name)
                        temporary.append((path, destination / f"{size}.jpg"))
                        variant.save(output, format="JPEG", quality=85, optimize=True)
                        output.flush()
                        os.fsync(output.fileno())
            for path, target in temporary:
                path.replace(target)
        finally:
            for path, _ in temporary:
                path.unlink(missing_ok=True)
