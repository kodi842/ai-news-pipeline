"""Уровень 1 дедупа: perceptual hash картинок (спека §3).

Одна и та же фотография у трёх пабликов — это одна новость, даже если тексты
переписаны. Но сжатие, ресайз и чужой вотермарк меняют байты, поэтому sha256
здесь бесполезен: нужен хэш, устойчивый к пережатию.
"""
import numpy as np
import pytest
from PIL import Image, ImageDraw

from tgtraff.dedup.phash import hamming, phash_file, phash_image


def _photo(seed: int = 1, size: int = 512) -> Image.Image:
    """Псевдо-фотография: крупные пятна вместо белого шума (шум после ресайза
    вырождается в серое поле и хэш от него бессмыслен, как у пустой картинки)."""
    rng = np.random.default_rng(seed)
    small = rng.integers(0, 255, size=(16, 16, 3), dtype=np.uint8)
    return Image.fromarray(small).resize((size, size), Image.BICUBIC)


def _save(img: Image.Image, path, **kw) -> str:
    img.save(path, **kw)
    return str(path)


# ---- устойчивость ----

def test_same_image_same_hash(tmp_path):
    img = _photo()
    a = phash_file(_save(img, tmp_path / "a.jpg", quality=95))
    b = phash_file(_save(img, tmp_path / "b.jpg", quality=95))
    assert a == b


def test_recompression_keeps_hash_close(tmp_path):
    """Паблик пережимает картинку — хэш почти не меняется."""
    img = _photo()
    a = phash_file(_save(img, tmp_path / "a.jpg", quality=95))
    b = phash_file(_save(img, tmp_path / "b.jpg", quality=35))
    assert hamming(a, b) <= 6


def test_resize_keeps_hash_close(tmp_path):
    img = _photo()
    a = phash_file(_save(img, tmp_path / "a.jpg", quality=90))
    b = phash_file(_save(img.resize((256, 256)), tmp_path / "b.jpg", quality=90))
    assert hamming(a, b) <= 6


def test_watermark_corner_keeps_hash_close(tmp_path):
    """Чужой вотермарк в углу не должен разводить одну картинку по двум кластерам.

    Вотермарк пабликов — полупрозрачная плашка в углу; именно она и проверяется.
    Непрозрачная заливка на четверть кадра — это уже другая картинка, и хэш
    честно должен разъехаться."""
    img = _photo()
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle([380, 470, 500, 500], fill=(255, 255, 255, 140))
    marked = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    a = phash_file(_save(img, tmp_path / "a.jpg", quality=90))
    b = phash_file(_save(marked, tmp_path / "b.jpg", quality=90))
    assert hamming(a, b) <= 10


def test_different_photos_are_far_apart(tmp_path):
    a = phash_file(_save(_photo(1), tmp_path / "a.jpg"))
    b = phash_file(_save(_photo(99), tmp_path / "b.jpg"))
    assert hamming(a, b) >= 16


# ---- формат хэша ----

def test_hash_is_16_hex_chars(tmp_path):
    h = phash_file(_save(_photo(), tmp_path / "a.jpg"))
    assert len(h) == 16 and all(c in "0123456789abcdef" for c in h)


def test_hamming_of_equal_is_zero():
    assert hamming("ffffffffffffffff", "ffffffffffffffff") == 0
    assert hamming("0000000000000000", "ffffffffffffffff") == 64
    assert hamming("0000000000000001", "0000000000000000") == 1


def test_hamming_of_missing_hash_is_max():
    assert hamming(None, "0000000000000000") == 64
    assert hamming("0000000000000000", None) == 64


# ---- что хэшировать нельзя ----

def test_flat_image_has_no_hash(tmp_path):
    """Однотонная заливка (заглушка, чёрный кадр) склеила бы всё подряд."""
    flat = Image.new("RGB", (400, 400), (18, 18, 18))
    assert phash_file(_save(flat, tmp_path / "flat.png")) is None


def test_broken_file_returns_none(tmp_path):
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not an image at all")
    assert phash_file(path) is None


def test_missing_file_returns_none(tmp_path):
    assert phash_file(tmp_path / "nope.jpg") is None


def test_video_file_returns_none(tmp_path):
    """Видео мы не хэшируем — это работа не фазы 3."""
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100)
    assert phash_file(path) is None


def test_grayscale_and_rgba_images_are_supported(tmp_path):
    rgba = _photo().convert("RGBA")
    gray = _photo().convert("L")
    assert phash_image(rgba) is not None
    assert phash_image(gray) is not None


def test_animated_gif_first_frame_is_hashed(tmp_path):
    frames = [_photo(3).resize((128, 128)), _photo(4).resize((128, 128))]
    path = tmp_path / "a.gif"
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=100)
    assert phash_file(path) is not None
