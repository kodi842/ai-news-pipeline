#!/usr/bin/env python3
"""Генератор аватарок сетки «ЧП [Город]» (Pillow, без нейросетей).

Два направления единого стиля:
  A «Плашка» — ведёт типографика: «ЧП» — герой знака, цвет города служебный.
  B «Молния» — ведёт пиктограмма: молния = скорость («приоритет молния» из спеки),
                «ЧП» и код города уходят в нижнюю плашку, цвет города — поле.

Оба знака рассчитаны на кружок 40 px в списке чатов: один доминирующий элемент,
никаких тонких линий, контраст не ниже 7:1.

Примеры:
  python3 avatars.py --all                  # всё: TG+VK, оба стиля, 3 города, контактные листы
  python3 avatars.py --style a --city magnitogorsk --size 512 --out ava.png
  python3 avatars.py --style b --code-style region --all
"""
from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw

import brand
from brand import CITIES, INK, INK_DEEP, PAPER

SS = 4  # суперсэмплинг: рисуем в 4× и уменьшаем — края и текст без «лесенки»

STYLES = ("a", "b")
STYLE_TITLES = {"a": "A — «Плашка»", "b": "B — «Молния»"}


def _shade(hex_color: str, factor: float):
    r, g, b = brand.hex_rgb(hex_color)
    return tuple(max(0, min(255, int(c * factor))) for c in (r, g, b))


def _bolt_polygon(cx: float, cy: float, w: float, h: float):
    """Молния, вписанная в бокс (cx, cy — центр бокса)."""
    unit = [
        (0.60, 0.00), (0.08, 0.575), (0.44, 0.575),
        (0.30, 1.00), (0.92, 0.400), (0.57, 0.400),
    ]
    x0, y0 = cx - w / 2.0, cy - h / 2.0
    return [(x0 + ux * w, y0 + uy * h) for ux, uy in unit]


# ------------------------------------------------------------- стиль A -----

def _style_a(size: int, city: dict, code_style: str) -> Image.Image:
    S = size * SS
    accent = city["accent"]
    im = Image.new("RGB", (S, S), INK)
    d = ImageDraw.Draw(im)

    band_top = int(S * 0.700)
    d.rectangle([0, band_top, S, S], fill=accent)

    ring = int(S * 0.030)

    # «ЧП» — герой знака. Габарит считается по хорде круга на верхней кромке
    # букв: аватарка везде обрезается в кружок, метрическая ширина тут не годится.
    label, tracking, cap_y, cap_h = "ЧП", 0.012, S * 0.352, S * 0.375
    limit = brand.chord(S, cap_y - cap_h / 2.0, inset=ring * 1.6)
    f = brand.fit_font("display", label, limit, start=int(S * 0.62),
                       max_height=cap_h, tracking=tracking)
    brand.draw_block(d, (S / 2, cap_y), label, f, PAPER,
                     tracking_px=tracking * f.size, stroke_width=int(S * 0.008))

    # Код города на плашке.
    code = city["region"] if code_style == "region" else city["code"]
    code_y = S * 0.808
    fc = brand.fit_font("bold", code, brand.chord(S, code_y, inset=ring * 3.0),
                        start=int(S * 0.20), max_height=S * 0.115, tracking=0.10)
    brand.draw_block(d, (S / 2, code_y), code, fc, INK, tracking_px=0.10 * fc.size)

    # Кольцо: на тёмной теме ТГ графитовый круг иначе растворяется в фоне.
    d.ellipse([ring // 2, ring // 2, S - ring // 2, S - ring // 2],
              outline=accent, width=ring)

    return im.resize((size, size), Image.LANCZOS)


# ------------------------------------------------------------- стиль B -----

def _style_b(size: int, city: dict, code_style: str) -> Image.Image:
    S = size * SS
    accent = city["accent"]
    im = Image.new("RGB", (S, S), accent)
    d = ImageDraw.Draw(im)

    band_top = int(S * 0.720)
    d.rectangle([0, band_top, S, S], fill=INK)
    ring = int(S * 0.024)

    d.polygon(_bolt_polygon(S * 0.5, S * 0.372, S * 0.50, S * 0.545), fill=INK)

    code = city["region"] if code_style == "region" else city["code"]
    label = "ЧП " + code
    label_y = S * 0.812
    fc = brand.fit_font("bold", label, brand.chord(S, label_y, inset=ring * 3.0),
                        start=int(S * 0.19), max_height=S * 0.105, tracking=0.07)
    brand.draw_block(d, (S / 2, label_y), label, fc, PAPER, tracking_px=0.07 * fc.size)

    d.ellipse([ring // 2, ring // 2, S - ring // 2, S - ring // 2],
              outline=INK, width=ring)

    return im.resize((size, size), Image.LANCZOS)


def avatar(style: str, city_key: str, size: int = 512,
           code_style: str = "code") -> Image.Image:
    city = CITIES[city_key]
    if style == "a":
        return _style_a(size, city, code_style)
    if style == "b":
        return _style_b(size, city, code_style)
    raise ValueError("Неизвестный стиль: %s" % style)


# ------------------------------------------------------- обложка VK --------

def vk_cover(style: str, city_key: str, title: str,
             size=(1590, 400), code_style: str = "code") -> Image.Image:
    """Обложка сообщества VK 1590×400.

    Композиция центрированная: мобильный VK режет обложку по краям, всё значимое
    должно жить в середине.
    """
    city = CITIES[city_key]
    accent = city["accent"]
    W, H = size[0] * 2, size[1] * 2
    im = Image.new("RGB", (W, H), INK)
    d = ImageDraw.Draw(im)

    for i in range(-H, W, int(H * 0.44)):        # диагональная штриховка акцентом
        d.polygon([(i, H), (i + int(H * 0.11), H),
                   (i + H + int(H * 0.11), 0), (i + H, 0)],
                  fill=_shade(accent, 0.26))
    d.rectangle([0, H - int(H * 0.05), W, H], fill=accent)

    mark = _circled(avatar(style, city_key, size=int(H * 0.58), code_style=code_style))
    gap = int(H * 0.13)
    sub = "происшествия · новости · погода · 24/7"
    ft = brand.fit_font("display", title, W * 0.52, start=int(H * 0.29))
    fs = brand.fit_font("regular", sub, W * 0.52, start=int(H * 0.115))
    tw = max(brand.text_width(ft, title), brand.text_width(fs, sub))

    x = int((W - (mark.width + gap + tw)) // 2)
    cy = int(H * 0.46)
    im.paste(mark, (x, cy - mark.height // 2), mark)
    tx = int(x + mark.width + gap)
    d.text((tx, cy - int(H * 0.055)), title, font=ft, fill=PAPER, anchor="ls")
    d.text((tx, cy + int(H * 0.115)), sub, font=fs, fill=brand.MUTED, anchor="ls")

    return im.resize(size, Image.LANCZOS)


# ------------------------------------------------------ контактный лист ----

def _circled(im: Image.Image) -> Image.Image:
    """Как ТГ показывает аватар: квадрат обрезается в круг."""
    m = Image.new("L", (im.width * 4, im.height * 4), 0)
    ImageDraw.Draw(m).ellipse([0, 0, m.width - 1, m.height - 1], fill=255)
    out = im.convert("RGBA")
    out.putalpha(m.resize(im.size, Image.LANCZOS))
    return out


def contact_sheet(style: str, code_style: str = "code") -> Image.Image:
    """Лист для выбора: знак крупно + проверка читаемости в кружках 40/64/96 px
    на светлой и тёмной теме Telegram."""
    W = 1500
    big, gap, pad = 340, 40, 60
    head, small_row = 120, 190
    H = head + big + 46 + small_row * 2 + pad
    im = Image.new("RGB", (W, H), "#FFFFFF")
    d = ImageDraw.Draw(im)

    d.text((pad, 44), "Аватарки — стиль %s" % STYLE_TITLES[style],
           font=brand.font("display", 46), fill=INK, anchor="lm")
    d.text((W - pad, 48), "код города: %s" % ("регион (74/42/35)" if code_style == "region"
                                              else "буквы (МГН/НВК/ЧЕР)"),
           font=brand.font("regular", 24), fill=brand.MUTED, anchor="rm")

    keys = list(CITIES)
    x0 = pad
    for i, k in enumerate(keys):
        x = x0 + i * (big + gap)
        av = avatar(style, k, size=big, code_style=code_style)
        im.paste(_circled(av), (x, head), _circled(av))
        d.text((x + big / 2, head + big + 26), CITIES[k]["name"],
               font=brand.font("bold", 27), fill=INK, anchor="mm")

    y = head + big + 60
    for theme, bg, fg in (("светлая тема", "#FFFFFF", INK), ("тёмная тема", "#17212B", PAPER)):
        d.rectangle([0, y, W, y + small_row], fill=bg)
        d.text((pad, y + small_row / 2), theme, font=brand.font("regular", 24),
               fill=fg, anchor="lm")
        x = pad + 210
        for k in keys:
            for px in (40, 64, 96):
                av = _circled(avatar(style, k, size=px * 3, code_style=code_style)
                              .resize((px, px), Image.LANCZOS))
                im.paste(av, (x, y + (small_row - px) // 2 - 12), av)
                d.text((x + px / 2, y + small_row - 26), "%d px" % px,
                       font=brand.font("regular", 17), fill=brand.MUTED, anchor="mm")
                x += px + 34
            x += 46
        y += small_row
    return im


# ------------------------------------------------------------------ CLI ----

def _save(im: Image.Image, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path)
    print("  " + os.path.relpath(path, brand.BRAND_DIR))


def main():
    p = argparse.ArgumentParser(description="Аватарки сетки «ЧП [Город]»")
    p.add_argument("--style", choices=STYLES)
    p.add_argument("--city", choices=list(CITIES))
    p.add_argument("--size", type=int, default=512)
    p.add_argument("--code-style", choices=("code", "region"), default="code",
                   help="что писать в плашке: буквенный код города или код региона")
    p.add_argument("--out")
    p.add_argument("--all", action="store_true", help="сгенерировать весь пакет")
    a = p.parse_args()

    root = os.path.join(brand.BRAND_DIR, "out", "avatars")

    if a.all:
        print("Аватарки:")
        for st in STYLES:
            for ck in CITIES:
                _save(avatar(st, ck, 512), os.path.join(root, st, "tg_%s_512.png" % ck))
                _save(avatar(st, ck, 1000), os.path.join(root, st, "vk_%s_1000.png" % ck))
                _save(vk_cover(st, ck, "ЧП " + CITIES[ck]["name"]),
                      os.path.join(root, st, "vk_cover_%s_1590x400.png" % ck))
            _save(contact_sheet(st), os.path.join(root, "CONTACT-SHEET-%s.png" % st))
            _save(contact_sheet(st, "region"),
                  os.path.join(root, "CONTACT-SHEET-%s-region.png" % st))
        return

    if not (a.style and a.city):
        p.error("нужны --style и --city (или --all)")
    _save(avatar(a.style, a.city, a.size, a.code_style),
          a.out or os.path.join(root, "%s_%s_%d.png" % (a.style, a.city, a.size)))


if __name__ == "__main__":
    main()
