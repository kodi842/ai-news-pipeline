"""Плашки клипа (VIDEO-SPEC): хук, бейдж города, субтитры, аутро с CTA.

Всё рисует Pillow из бренд-пакета R0 (`assets/brand`) — тот же графит, тот же
PT Sans и тот же приём «плашка», что у аватарок и обложек постов. Никакой
генерации нейросетями (жёсткое правило 4): в кадре только типографика,
геометрия и реальные кадры кластера.

Почему накладки — картинки, а не `drawtext`/`subtitles` в ffmpeg: шрифт бренда
и перенос строк тогда зависели бы от fontconfig и libass внутри контейнера, а
это самая тихая из возможных поломок — клип соберётся, но текст уедет
системным шрифтом или исчезнет. Здесь же плашка либо нарисована, либо нет.

Где что стоит: низ кадра у ВК/Rutube/OK занимает интерфейс (кнопки, описание),
поэтому субтитры живут в районе 60-80% высоты, а не у нижней кромки. Хук —
в верхней трети, центр кадра всегда открыт: под накладкой живое видео.
"""
from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

from PIL import Image, ImageDraw

log = logging.getLogger("tgtraff.clips.overlay")

BRAND_DIR = Path(__file__).resolve().parents[3] / "assets" / "brand"

# Куда что ставим (доли высоты кадра).
HOOK_TOP = 0.135
BADGE_TOP = 0.030
SUBTITLE_CENTER = 0.700
# Ниже этой линии — интерфейс площадки: туда не пишем ничего.
SAFE_BOTTOM = 0.860

_MAX_HOOK_LINES = 3
_MAX_SUB_LINES = 3


def hook_plate(city: str, hook: str, out_dir, settings, city_name: str | None = None):
    """Плашка хука + локап канала. Прозрачный кадр поверх видео."""
    return _cached(out_dir, "hook", city, hook, settings,
                   lambda: _draw_hook(city, hook, settings, city_name))


def badge_plate(city: str, out_dir, settings, city_name: str | None = None):
    """Бейдж города на весь клип: знак канала и название."""
    return _cached(out_dir, "badge", city, "", settings,
                   lambda: _draw_badge(city, settings, city_name))


def subtitle_plate(city: str, text: str, out_dir, settings):
    """Одна реплика субтитров. None — писать нечего."""
    if not (text or "").strip():
        return None
    return _cached(out_dir, "sub", city, text, settings,
                   lambda: _draw_subtitle(city, text, settings))


def outro_frame(city: str, cta: str, out_dir, settings, city_name: str | None = None):
    """Аутро: непрозрачный кадр с CTA и юзернеймом — последняя секунда клипа."""
    return _cached(out_dir, "outro", city, cta, settings,
                   lambda: _draw_outro(city, cta, settings, city_name))


# ---------- рисование ----------

def _draw_hook(city: str, hook: str, settings, city_name: str | None):
    brand, palette = _brand(city)
    W, H = settings.width, settings.height
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    pad = int(W * 0.055)
    text_w = W - pad * 2 - int(W * 0.06)
    font_size = int(H * 0.052)
    f = brand.font("display", font_size) if brand else None
    lines = _wrap(hook.upper(), f, text_w, _MAX_HOOK_LINES, brand)

    line_h = int(font_size * 1.16)
    box_h = line_h * len(lines) + int(H * 0.026)
    top = int(H * HOOK_TOP)
    # Плашка: тёмная подложка + ребро акцентом города. Тёмная — потому что под
    # ней живое видео, и белый текст на видео без подложки не читается.
    d.rectangle([0, top, W, top + box_h], fill=_rgba(palette["ink_deep"], 214))
    d.rectangle([0, top, int(W * 0.017), top + box_h], fill=_rgb(palette["accent"]))

    y = top + int(H * 0.013)
    for line in lines:
        if brand:
            brand.draw_block(d, (W / 2, y + line_h / 2), line, f, _rgb(palette["paper"]),
                             tracking_px=0.01 * font_size)
        else:                                     # бренд-пакета нет — рисуем как умеем
            d.text((W / 2, y + line_h / 2), line, fill=_rgb(palette["paper"]), anchor="mm")
        y += line_h
    return im


def _draw_badge(city: str, settings, city_name: str | None):
    brand, palette = _brand(city)
    W, H = settings.width, settings.height
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    pad = int(W * 0.055)
    top = int(H * BADGE_TOP)
    mark_size = int(H * 0.052)
    label = f"ЧП {(city_name or _city_name(city)).upper()}"
    size = int(H * 0.024)
    f = brand.font("bold", size) if brand else None

    # Тёмная подложка под бейджем: он висит весь клип поверх ЧУЖОГО кадра,
    # а кадр бывает светлым (снег, небо, стена) — белые буквы на нём исчезают.
    label_w = brand.text_width(f, label, 0.045 * size) if brand else len(label) * size * 0.6
    x = pad + mark_size + int(W * 0.022)
    d.rounded_rectangle([pad - int(W * 0.018), top - int(H * 0.010),
                         x + label_w + int(W * 0.030), top + mark_size + int(H * 0.010)],
                        radius=int(H * 0.019), fill=_rgba(palette["ink_deep"], 150))

    mark = _mark(city, mark_size)
    if mark is not None:
        im.paste(mark, (pad, top), mark)

    if brand:
        brand.draw_block(d, (x + label_w / 2, top + mark_size / 2), label, f,
                         _rgb(palette["paper"]), tracking_px=0.045 * size)
    else:
        d.text((x, top + mark_size / 2), label, fill=_rgb(palette["paper"]), anchor="lm")
    return im


def _draw_subtitle(city: str, text: str, settings):
    brand, palette = _brand(city)
    W, H = settings.width, settings.height
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    pad = int(W * 0.06)
    size = int(H * 0.030)
    f = brand.font("bold", size) if brand else None
    lines = _wrap(text, f, W - pad * 2, _MAX_SUB_LINES, brand)
    line_h = int(size * 1.34)
    box_h = line_h * len(lines) + int(H * 0.018)

    center = int(H * SUBTITLE_CENTER)
    top = center - box_h // 2
    bottom = min(top + box_h, int(H * SAFE_BOTTOM))
    top = bottom - box_h
    d.rounded_rectangle([pad // 2, top, W - pad // 2, bottom],
                        radius=int(H * 0.010), fill=_rgba(palette["ink_deep"], 190))

    y = top + int(H * 0.009)
    for line in lines:
        if brand:
            brand.draw_block(d, (W / 2, y + line_h / 2), line, f, _rgb(palette["paper"]))
        else:
            d.text((W / 2, y + line_h / 2), line, fill=_rgb(palette["paper"]), anchor="mm")
        y += line_h
    return im


def _draw_outro(city: str, cta: str, settings, city_name: str | None):
    brand, palette = _brand(city)
    W, H = settings.width, settings.height
    im = Image.new("RGBA", (W, H), _rgba(palette["ink"], 255))
    d = ImageDraw.Draw(im)

    # Полоса акцента города — та же деталь, что на обложках постов.
    d.rectangle([0, int(H * 0.46), W, int(H * 0.462) + int(H * 0.004)],
                fill=_rgb(palette["accent"]))

    mark = _mark(city, int(H * 0.14))
    if mark is not None:
        im.paste(mark, ((W - mark.width) // 2, int(H * 0.26)), mark)

    lines = [ln.strip() for ln in (cta or "").splitlines() if ln.strip()]
    y = int(H * 0.52)
    for i, line in enumerate(lines):
        handle = line.startswith("@")
        size = int(H * (0.040 if handle else 0.034))
        f = brand.font("display" if handle else "bold", size) if brand else None
        color = _rgb(palette["accent"] if handle else palette["paper"])
        if brand:
            f = brand.fit_font("display" if handle else "bold", line, W * 0.84, start=size)
            brand.draw_block(d, (W / 2, y + size * 0.7), line, f, color)
        else:
            d.text((W / 2, y + size * 0.7), line, fill=color, anchor="mm")
        y += int(size * (1.9 if i == 0 else 1.5))
    return im


# ---------- бренд ----------

def _brand(city: str):
    """Модуль бренда и палитра города. Бренд-пакета может не быть (или города
    в нём) — тогда рисуем нейтральным графитом: клип важнее плашки."""
    palette = {"ink": "#14171C", "ink_deep": "#0B0D10", "paper": "#FFFFFF",
               "accent": "#E63329"}
    brand = _import_brand()
    if brand is None:
        return None, palette
    city_conf = getattr(brand, "CITIES", {}).get(city)
    palette.update({"ink": brand.INK, "ink_deep": brand.INK_DEEP, "paper": brand.PAPER})
    if city_conf:
        palette["accent"] = city_conf["accent"]
    return brand, palette


def _import_brand():
    try:
        if str(BRAND_DIR) not in sys.path:
            sys.path.insert(0, str(BRAND_DIR))
        import brand                              # assets/brand/brand.py

        brand.font("bold", 20)                    # шрифты на месте?
        return brand
    except Exception as exc:                      # noqa: BLE001 — плашка не критична
        log.warning("бренд-пакет недоступен (%s) — плашки будут без фирменного шрифта", exc)
        return None


def _mark(city: str, size: int):
    """Круглый знак канала — тот же, что стоит аватаркой (avatars.py)."""
    try:
        if str(BRAND_DIR) not in sys.path:
            sys.path.insert(0, str(BRAND_DIR))
        import avatars

        mark = avatars.avatar("a", city, size=size).convert("RGBA")
    except Exception as exc:                      # noqa: BLE001
        log.debug("знак канала не нарисовался (%s)", exc)
        return None
    mask = Image.new("L", (mark.width * 4,) * 2, 0)
    ImageDraw.Draw(mask).ellipse([0, 0, mask.width - 1, mask.width - 1], fill=255)
    mark.putalpha(mask.resize(mark.size, Image.LANCZOS))
    return mark


def _city_name(city: str) -> str:
    brand = _import_brand()
    conf = getattr(brand, "CITIES", {}).get(city) if brand else None
    return conf["name"] if conf else city


# ---------- служебное ----------

def _wrap(text: str, font, max_width: float, max_lines: int, brand) -> list[str]:
    """Перенос по словам под ширину плашки. Последняя строка при переборе
    обрезается многоточием: лучше короче, чем за краем кадра."""
    words = (text or "").split()
    if not words:
        return [""]
    if font is None or brand is None:
        # Без метрик шрифта — грубая разбивка по числу знаков.
        step = max(12, int(max_width / 22))
        lines, current = [], ""
        for word in words:
            probe = f"{current} {word}".strip()
            if len(probe) > step and current:
                lines.append(current)
                current = word
            else:
                current = probe
        lines.append(current)
        return lines[:max_lines]

    lines: list[str] = []
    current = ""
    for word in words:
        probe = f"{current} {word}".strip()
        if brand.text_width(font, probe) > max_width and current:
            lines.append(current)
            current = word
        else:
            current = probe
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" ,.;") + "…"
    return lines


def _cached(out_dir, kind: str, city: str, text: str, settings, draw):
    """Плашка кэшируется по содержимому: рендер идёт минутами, второй раз
    ту же картинку не рисуем."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(
        f"{kind}|{city}|{text}|{settings.width}x{settings.height}".encode()).hexdigest()[:12]
    path = out / f"{kind}_{city}_{key}.png"
    if path.exists() and path.stat().st_size:
        return path
    image = draw()
    image.save(path)
    return path


def _rgb(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _rgba(value: str, alpha: int):
    return (*_rgb(value), alpha)
