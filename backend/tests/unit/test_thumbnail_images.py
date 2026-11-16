from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.services.nas import Access, LocalFs, Region
from app.services.thumbs.images import ImageDecodeError, VARIANT_SIZES, render_variants


def image_bytes(mode: str = "RGB", size: tuple[int, int] = (2000, 1000), color: str | tuple[int, ...] = "red", *, image_format: str = "JPEG") -> BytesIO:
    stream = BytesIO()
    with Image.new(mode, size, color) as image:
        image.save(stream, format=image_format)
    stream.seek(0)
    return stream


def test_exif_orientation_three_sizes_and_no_metadata(tmp_path: Path) -> None:
    stream = BytesIO()
    with Image.new("RGB", (2000, 1000), "red") as image:
        image.paste("blue", (1000, 0, 2000, 1000))
        exif = Image.Exif()
        exif[274] = 6
        exif[271] = "拍摄设备信息"
        exif[315] = "保密作者"
        image.save(stream, format="JPEG", exif=exif, icc_profile=b"private-profile", comment=b"private-comment")
    stream.seek(0)
    render_variants(stream, tmp_path)
    expected = {300: (150, 300), 1600: (800, 1600), 1024: (512, 1024)}
    for size in VARIANT_SIZES:
        with Image.open(tmp_path / f"{size}.jpg") as result:
            assert result.format == "JPEG" and result.mode == "RGB"
            assert result.size == expected[size]
            assert result.getpixel((result.width // 2, result.height // 4))[0] > 240
            assert result.getpixel((result.width // 2, result.height * 3 // 4))[2] > 240
            assert not result.getexif()
            assert not {"exif", "icc_profile", "xmp", "comment"}.intersection(result.info)


def test_small_image_is_not_upscaled(tmp_path: Path) -> None:
    render_variants(image_bytes(size=(80, 40)), tmp_path)
    for size in VARIANT_SIZES:
        with Image.open(tmp_path / f"{size}.jpg") as result:
            assert result.size == (80, 40)


@pytest.mark.parametrize("mode,color", [("RGBA", (255, 0, 0, 0)), ("LA", (0, 0))])
def test_transparency_is_composited_over_white(tmp_path: Path, mode: str, color: tuple[int, ...]) -> None:
    render_variants(image_bytes(mode, (20, 20), color, image_format="PNG"), tmp_path)
    with Image.open(tmp_path / "300.jpg") as result:
        assert result.getpixel((10, 10)) == (255, 255, 255)


def test_palette_transparency_is_composited_over_white(tmp_path: Path) -> None:
    stream = BytesIO()
    with Image.new("P", (20, 20), 0) as image:
        image.save(stream, format="PNG", transparency=0)
    stream.seek(0)
    render_variants(stream, tmp_path)
    with Image.open(tmp_path / "300.jpg") as result:
        assert result.getpixel((10, 10)) == (255, 255, 255)


def test_animated_png_uses_first_frame(tmp_path: Path) -> None:
    stream = BytesIO()
    with Image.new("RGB", (40, 20), "red") as first, Image.new("RGB", (40, 20), "blue") as second:
        first.save(stream, format="PNG", save_all=True, append_images=[second], duration=100, loop=0)
    stream.seek(0)
    render_variants(stream, tmp_path)
    with Image.open(tmp_path / "300.jpg") as result:
        red, _, blue = result.getpixel((20, 10))
        assert red > 240 and blue < 10


@pytest.mark.parametrize("mode,color", [("L", 80), ("CMYK", (0, 255, 255, 0))])
def test_gray_and_cmyk_are_converted_to_rgb(tmp_path: Path, mode: str, color: int | tuple[int, ...]) -> None:
    stream = BytesIO()
    with Image.new(mode, (200, 100), color) as image:
        image.save(stream, format="JPEG")
    stream.seek(0)
    render_variants(stream, tmp_path)
    with Image.open(tmp_path / "1024.jpg") as result:
        assert result.mode == "RGB" and result.size == (200, 100)


@pytest.mark.parametrize("content", [b"not a jpeg /private/nas", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"])
def test_broken_image_reports_safe_error_without_files(tmp_path: Path, content: bytes) -> None:
    with pytest.raises(ImageDecodeError, match="图片损坏或无法解码") as error:
        render_variants(BytesIO(content), tmp_path)
    assert "/private/nas" not in str(error.value)
    assert list(tmp_path.iterdir()) == []


def test_decompression_bomb_is_rejected_before_pixel_decode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stream = image_bytes(size=(200, 100))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(ImageDecodeError, match="图片像素过大"):
        render_variants(stream, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_output_failure_cleans_temporary_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save = Image.Image.save
    calls = 0
    def failing_save(image: Image.Image, *args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("模拟磁盘写入失败")
        save(image, *args, **kwargs)
    stream = image_bytes(size=(200, 100))
    monkeypatch.setattr(Image.Image, "save", failing_save)
    with pytest.raises(OSError, match="模拟磁盘写入失败"):
        render_variants(stream, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_concurrent_reruns_publish_valid_jpegs_without_temporary_files(tmp_path: Path) -> None:
    def render(_: int) -> None:
        render_variants(image_bytes(size=(800, 600)), tmp_path)
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(render, range(3)))
    assert sorted(path.name for path in tmp_path.iterdir()) == ["1024.jpg", "1600.jpg", "300.jpg"]
    for size in VARIANT_SIZES:
        with Image.open(tmp_path / f"{size}.jpg") as image:
            image.load()
            assert max(image.size) <= size


def test_nas_stream_is_passed_directly_to_decoder(tmp_path: Path) -> None:
    with image_bytes(size=(200, 100)) as original:
        data = original.read()
    fs = LocalFs(tmp_path / "nas", {Region.TEMP: Access.WRITE})
    try:
        with fs.open_write("工作台临时区/image.jpg") as output:
            output.write(data)
        with fs.open_read("工作台临时区/image.jpg") as source:
            render_variants(source, tmp_path / "thumbs")
        assert (tmp_path / "thumbs/300.jpg").is_file()
    finally:
        fs.close()
