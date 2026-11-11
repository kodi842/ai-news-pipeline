"""Ступень 3 медиа-каскада: своя шаблонная обложка (спека §5).

Генератор уже написан в бренд-пакете R0 (`assets/brand/covers.py`, Pillow,
никаких нейросетей) — здесь мостик к нему: конвейеру нужен путь к готовому
файлу по (город, категория, район, заголовок).

Карточки нарисованы для всех семи категорий rewriter'а: `crime` и `other`
дорисованы по решению ревью §30 (щит и нейтральный знак), подмены
«криминал → город» больше нет.
"""
from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

log = logging.getLogger("tgtraff.rewriter.cover")

BRAND_DIR = Path(__file__).resolve().parents[3] / "assets" / "brand"
# Категории rewriter'а → категории обложек R0 (с §30 — одна к одной).
CATEGORY_MAP = {
    "dtp": "dtp", "fire": "fire", "utility": "utility", "weather": "weather",
    "city": "city", "crime": "crime", "other": "other",
}


def render_cover(city: str, category: str, district: str = "", title: str = "",
                 out_dir: Path | str = "data/covers") -> Path | None:
    """Рисует обложку и возвращает путь. None — генератор недоступен (нет шрифтов
    или бренд-пакета): тогда драфт уходит модератору без картинки."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{city}|{category}|{district}|{title}".encode()).hexdigest()[:12]
    path = out / f"{city}_{CATEGORY_MAP.get(category, 'city')}_{key}.png"
    if path.exists():
        return path

    try:
        image = _render(city, category, district, title)
    except Exception as exc:                    # noqa: BLE001 — обложка не критична
        log.warning("шаблонная обложка не нарисовалась (%s)", exc)
        return None
    image.save(path)
    return path


def _render(city: str, category: str, district: str, title: str):
    if str(BRAND_DIR) not in sys.path:
        sys.path.insert(0, str(BRAND_DIR))
    import covers                                # assets/brand/covers.py

    return covers.cover(city_key=city, category=CATEGORY_MAP.get(category, "city"),
                        district=district or "", title=title or "")
