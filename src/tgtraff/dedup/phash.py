"""Perceptual hash картинок — вторая половина уровня 1 (спека §3).

Зачем не sha256: одна и та же фотография у трёх пабликов приходит пережатой,
отресайзенной и иногда с чужим вотермарком — байты разные, картинка одна.
pHash (DCT по низким частотам) переживает всё это и даёт 64 бита, по которым
расстояние Хэмминга показывает «та же картинка / другая».

Реализация своя, на numpy: `imagehash` тянет scipy ради одного DCT — это
30 строк кода против 60 МБ зависимости.
"""
from __future__ import annotations

import functools
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile

# Пережатые картинки из лент иногда обрезаны — лучше хэш по неполному файлу,
# чем пропуск целого медиа.
ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 200_000_000

HASH_BITS = 8            # 8×8 = 64 бита
IMAGE_SIZE = 32          # сторона картинки перед DCT (4× от hash_size)
MAX_DISTANCE = HASH_BITS * HASH_BITS
# Ниже этого разброса яркостей картинка считается пустой: однотонная заливка,
# чёрный кадр, белая заглушка. Хэш от неё склеил бы несвязанные новости.
MIN_STD = 3.0


@functools.lru_cache(maxsize=4)
def _dct_matrix(n: int) -> np.ndarray:
    """Матрица DCT-II: dct(x) = D @ x. Масштаб не важен — сравниваем с медианой."""
    k = np.arange(n).reshape(-1, 1)
    i = np.arange(n).reshape(1, -1)
    return np.cos(np.pi * (2 * i + 1) * k / (2 * n))


def phash_image(image: Image.Image) -> str | None:
    """64-битный хэш картинки в hex. None — картинка без информации."""
    try:
        gray = image.convert("L").resize((IMAGE_SIZE, IMAGE_SIZE), Image.LANCZOS)
    except (OSError, ValueError):
        return None

    pixels = np.asarray(gray, dtype=np.float64)
    if pixels.std() < MIN_STD:
        return None

    d = _dct_matrix(IMAGE_SIZE)
    dct = d @ pixels @ d.T
    low = dct[:HASH_BITS, :HASH_BITS]

    # Нулевую гармонику (среднюю яркость) в медиану не берём: она сдвигается
    # от любой коррекции экспозиции и уводит половину битов.
    flat = low.flatten()
    median = float(np.median(flat[1:]))
    bits = flat > median

    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def phash_file(path: str | Path | None) -> str | None:
    """Хэш файла с диска. Битый файл, не-картинка, отсутствующий путь → None."""
    if not path:
        return None
    try:
        with Image.open(path) as img:
            img.load()
            return phash_image(img)
    except Exception:      # noqa: BLE001 — медиа-файл не должен ронять конвейер
        return None


def hamming(a: str | None, b: str | None) -> int:
    """Расстояние между хэшами. Отсутствующий хэш — максимально далеко."""
    if not a or not b:
        return MAX_DISTANCE
    try:
        return int(int(a, 16) ^ int(b, 16)).bit_count()
    except ValueError:
        return MAX_DISTANCE
