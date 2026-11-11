#!/usr/bin/env python3
"""Шаблонные обложки постов для сетки «ЧП [Город]» (Pillow, без нейросетей).

Ступень 3 медиа-каскада из PROJECT-SPEC.md §5: чистого медиа в кластере нет —
пост выходит со своей карточкой. Никакой генерации изображений нейросетями.

Единство с аватарками держится на трёх вещах: тот же графит + шрифт PT Sans,
тот же приём «плашка» (акцент под тёмным текстом) и тот же знак в шапке.

Композиция адаптивная:
  без заголовка — герой карточки название категории (для «молний» и коротких);
  с заголовком  — герой заголовок, категория уходит в плашку-чип.

Примеры:
  python3 covers.py --city magnitogorsk --category dtp --district "Ленинский район"
  python3 covers.py --city cherepovets --category fire --district "Зашекснинский" \
      --title "Горит расселённый дом на Наседкина" --out fire.png
  python3 covers.py --sheet          # контактный лист по всем категориям
"""
from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw

import brand
from brand import CATEGORIES, CITIES, INK, INK_DEEP, MUTED, PAPER, PAPER_DIM
import avatars

W, H = 1280, 720          # 16:9 — максимум, который Telegram показывает без пережатия
SS = 2


def _shade(hex_color, factor):
    r, g, b = brand.hex_rgb(hex_color)
    return tuple(max(0, min(255, int(c * factor))) for c in (r, g, b))


# ------------------------------------------------------------- иконки ------

def _icon_shapes(name: str):
    """Пиктограмма категории в единичном квадрате: список примитивов."""
    if name == "warning":                                   # ДТП
        return [("poly", [(0.50, 0.05), (0.98, 0.90), (0.02, 0.90)]),
                ("hole", [(0.50, 0.30), (0.585, 0.45), (0.585, 0.62),
                          (0.415, 0.62), (0.415, 0.45)]),
                ("hole_ellipse", (0.435, 0.68, 0.565, 0.81))]
    if name == "flame":                                     # пожар
        return [("poly", [(0.50, 0.02), (0.67, 0.23), (0.75, 0.13), (0.86, 0.42),
                          (0.85, 0.66), (0.68, 0.90), (0.50, 0.99), (0.32, 0.90),
                          (0.15, 0.66), (0.14, 0.42), (0.29, 0.15), (0.37, 0.26)]),
                ("hole", [(0.50, 0.44), (0.66, 0.66), (0.62, 0.84), (0.50, 0.92),
                          (0.38, 0.84), (0.34, 0.66)])]
    if name == "drop":                                      # коммуналка
        return [("poly", [(0.50, 0.02), (0.20, 0.58), (0.80, 0.58)]),
                ("ellipse", (0.20, 0.34, 0.80, 0.96))]
    if name == "cloud":                                     # погода
        return [("ellipse", (0.10, 0.42, 0.48, 0.80)),
                ("ellipse", (0.30, 0.24, 0.76, 0.70)),
                ("ellipse", (0.58, 0.44, 0.92, 0.78)),
                ("rect", (0.16, 0.60, 0.88, 0.80))]
    if name == "tower":                                     # город
        return [("rect", (0.06, 0.46, 0.34, 0.96)),
                ("rect", (0.40, 0.20, 0.66, 0.96)),
                ("rect", (0.72, 0.58, 0.96, 0.96)),
                ("rect", (0.515, 0.04, 0.545, 0.20))]
    if name == "shield":                                    # криминал
        # Щит с замочной скважиной: правопорядок без наручников и полосатых роб —
        # карточка стоит под настоящей новостью, драматизировать её нечем.
        return [("poly", [(0.50, 0.02), (0.93, 0.17), (0.93, 0.52),
                          (0.50, 0.98), (0.07, 0.52), (0.07, 0.17)]),
                ("hole_ellipse", (0.415, 0.30, 0.585, 0.47)),
                ("hole", [(0.455, 0.42), (0.545, 0.42), (0.60, 0.72), (0.40, 0.72)])]
    if name == "info":                                      # прочее
        # Нейтральный знак: круг с восклицательным «i» — годится и происшествию,
        # и объявлению, ни на что не намекает.
        return [("ellipse", (0.02, 0.02, 0.98, 0.98)),
                ("hole_ellipse", (0.435, 0.16, 0.565, 0.29)),
                ("hole", [(0.435, 0.37), (0.565, 0.37), (0.565, 0.85), (0.435, 0.85)])]
    return []


def icon(name: str, size: int, color, alpha: int = 255) -> Image.Image:
    S = size * 3
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    rgba = tuple(color) + (alpha,) if isinstance(color, tuple) else \
        brand.hex_rgb(color) + (alpha,)
    for kind, geom in _icon_shapes(name):
        if kind == "poly":
            d.polygon([(x * S, y * S) for x, y in geom], fill=rgba)
        elif kind == "ellipse":
            d.ellipse([g * S for g in geom], fill=rgba)
        elif kind == "rect":
            d.rectangle([g * S for g in geom], fill=rgba)
        elif kind == "hole":
            d.polygon([(x * S, y * S) for x, y in geom], fill=(0, 0, 0, 0))
        elif kind == "hole_ellipse":
            d.ellipse([g * S for g in geom], fill=(0, 0, 0, 0))
    return im.resize((size, size), Image.LANCZOS)


# ------------------------------------------------------------- вёрстка -----

def _wrap(text: str, f, max_width: float, max_lines: int = 3):
    """Разбить строку по словам. Возвращает (строки, влезло_целиком)."""
    words, lines, cur = text.split(), [], ""
    for i, w in enumerate(words):
        probe = (cur + " " + w).strip()
        if brand.text_width(f, probe) <= max_width or not cur:
            cur = probe
        else:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                return lines, False
    if cur:
        lines.append(cur)
    return lines, True


def cover(city_key: str, category: str, district: str = "",
          title: str = "", handle: str = None, avatar_style: str = "a") -> Image.Image:
    city, cat = CITIES[city_key], CATEGORIES[category]
    if handle is None:
        handle = "@" + city["tg"]
    accent, cat_color = city["accent"], cat["color"]
    Wp, Hp = W * SS, H * SS
    im = Image.new("RGB", (Wp, Hp), INK)
    d = ImageDraw.Draw(im)

    # Фактура: диагональная штриховка цветом категории — держит карточку «своей»,
    # но не спорит с типографикой.
    for i in range(-Hp, Wp + Hp, int(Hp * 0.30)):
        d.polygon([(i, Hp), (i + int(Hp * 0.085), Hp),
                   (i + Hp + int(Hp * 0.085), 0), (i + Hp, 0)],
                  fill=_shade(cat_color, 0.17))

    ic = icon(cat["icon"], int(Hp * 0.70), cat_color, alpha=46)
    im.paste(ic, (int(Wp - ic.width * 0.78), int(Hp * 0.415 - ic.height / 2)), ic)

    d.rectangle([0, 0, int(Wp * 0.011), Hp], fill=cat_color)   # ребро категории

    pad = int(Wp * 0.055)

    # --- шапка: знак канала + название + код региона -----------------------
    mark = avatars.avatar(avatar_style, city_key, size=int(Hp * 0.115))
    mask = Image.new("L", (mark.width * 4,) * 2, 0)
    ImageDraw.Draw(mask).ellipse([0, 0, mask.width - 1, mask.width - 1], fill=255)
    mark.putalpha(mask.resize(mark.size, Image.LANCZOS))
    top = int(Hp * 0.072)
    im.paste(mark, (pad, top), mark)

    lock = "ЧП " + city["name"].upper()
    fl = brand.fit_font("bold", lock, Wp * 0.52, start=int(Hp * 0.062), tracking=0.045)
    brand.draw_block(d, (pad + mark.width + int(Wp * 0.021) +
                         brand.text_width(fl, lock, 0.045 * fl.size) / 2,
                         top + mark.height / 2), lock, fl, PAPER,
                     tracking_px=0.045 * fl.size)

    badge_w, badge_h = int(Wp * 0.075), int(Hp * 0.082)
    bx, by = Wp - pad - badge_w, top + (mark.height - badge_h) // 2
    d.rounded_rectangle([bx, by, bx + badge_w, by + badge_h],
                        radius=int(badge_h * 0.24), fill=accent)
    fr = brand.fit_font("bold", city["region"], badge_w * 0.62, start=int(badge_h * 0.72),
                        max_height=badge_h * 0.46)
    brand.draw_block(d, (bx + badge_w / 2, by + badge_h / 2), city["region"], fr, INK)

    # --- подвал: район и хэндл ---------------------------------------------
    foot_h = int(Hp * 0.135)
    field_bottom = Hp - foot_h - int(Hp * 0.075)
    d.rectangle([0, Hp - foot_h, Wp, Hp], fill=INK_DEEP)
    d.rectangle([0, Hp - foot_h, Wp, Hp - foot_h + int(Hp * 0.006)], fill=cat_color)
    fy = Hp - foot_h / 2
    ff = brand.font("bold", int(Hp * 0.050))

    if district:
        px_, py_ = pad, fy
        r = int(Hp * 0.017)                                   # метка-«булавка»
        d.ellipse([px_, py_ - r * 2.0, px_ + r * 2, py_], fill=cat_color)
        d.polygon([(px_, py_ - r * 0.7), (px_ + r * 2, py_ - r * 0.7),
                   (px_ + r, py_ + r * 1.5)], fill=cat_color)
        d.text((px_ + r * 3.4, fy), district, font=ff, fill=PAPER_DIM, anchor="lm")

    if handle:
        fh = brand.font("regular", int(Hp * 0.044))
        d.text((Wp - pad, fy), handle, font=fh, fill=MUTED, anchor="rm")

    # --- смысловой блок -----------------------------------------------------
    text_w = Wp * 0.80

    if title:
        chip_f = brand.fit_font("bold", cat["label"], text_w, start=int(Hp * 0.055),
                                tracking=0.09)
        chip_w = brand.text_width(chip_f, cat["label"], 0.09 * chip_f.size) + int(Hp * 0.062)
        chip_h = int(Hp * 0.088)

        # Заголовок — герой: берём самый крупный кегль, при котором текст влезает
        # целиком не более чем в 3 строки. Подбор по обёрнутому тексту, не по
        # длине одной строки, иначе кегль схлопывается до нечитаемого.
        head_bottom = top + mark.height + int(Hp * 0.075)
        avail = field_bottom - head_bottom - chip_h - int(Hp * 0.050)
        size = int(Hp * 0.135)
        while size > 26:
            ft = brand.font("display", size)
            lines, whole = _wrap(title, ft, text_w)
            if whole and len(lines) * size * 1.07 <= avail:
                break
            size -= 3
        lh = size * 1.07
        y = field_bottom - lh * len(lines) + (lh - size) / 2

        cy = y - chip_h - int(Hp * 0.050)
        d.rounded_rectangle([pad, cy, pad + chip_w, cy + chip_h],
                            radius=int(chip_h * 0.22), fill=cat_color)
        brand.draw_block(d, (pad + chip_w / 2, cy + chip_h / 2), cat["label"], chip_f,
                         INK, tracking_px=0.09 * chip_f.size)
        for i, ln in enumerate(lines):
            d.text((pad, y + i * lh), ln, font=ft, fill=PAPER, anchor="la")
    else:
        # Заголовка нет — герой само название категории.
        fc = brand.fit_font("display", cat["label"], text_w, start=int(Hp * 0.46),
                            max_height=Hp * 0.42, tracking=-0.005)
        b = brand.ink_box(fc, cat["label"], -0.005 * fc.size)
        brand.draw_block(d, (pad + (b[2] - b[0]) / 2, field_bottom - (b[3] - b[1]) / 2),
                         cat["label"], fc, cat_color, tracking_px=-0.005 * fc.size)
        sub = "%s · оперативно" % city["name"]
        fs = brand.font("regular", int(Hp * 0.052))
        d.text((pad, field_bottom - (b[3] - b[1]) - int(Hp * 0.055)), sub,
               font=fs, fill=MUTED, anchor="ls")

    return im.resize((W, H), Image.LANCZOS)


# --------------------------------------------------------- контактный лист -

def sheet() -> Image.Image:
    demo = [
        ("magnitogorsk", "dtp", "Ленинский район", ""),
        ("magnitogorsk", "fire", "Правобережный район",
         "На проспекте Карла Маркса горит машина, движение перекрыто"),
        ("novokuznetsk", "utility", "Заводской район",
         "Без горячей воды остались 42 дома в Заводском"),
        ("novokuznetsk", "weather", "", ""),
        ("cherepovets", "city", "Зашекснинский район",
         "Мост через Шексну закроют на ремонт с понедельника"),
        ("cherepovets", "dtp", "Индустриальный район",
         "Такси вылетело на тротуар у «Макси», есть пострадавшие"),
        ("magnitogorsk", "crime", "Орджоникидзевский район",
         "Из квартиры на Труда вынесли сейф с документами"),
        ("novokuznetsk", "other", "Центральный район", ""),
    ]
    cols, cw, gap, pad, head = 2, 620, 34, 46, 108
    rows = (len(demo) + cols - 1) // cols
    chh = int(cw * H / W)
    im = Image.new("RGB", (pad * 2 + cols * cw + (cols - 1) * gap,
                           head + rows * (chh + 58) + pad), "#FFFFFF")
    d = ImageDraw.Draw(im)
    d.text((pad, 52), "Шаблонные обложки постов — прототип",
           font=brand.font("display", 44), fill=INK, anchor="lm")
    for i, (ck, cat, dis, ttl) in enumerate(demo):
        x = pad + (i % cols) * (cw + gap)
        y = head + (i // cols) * (chh + 58)
        c = cover(ck, cat, dis, ttl).resize((cw, chh), Image.LANCZOS)
        im.paste(c, (x, y))
        cap = "%s · %s%s" % (CITIES[ck]["name"], CATEGORIES[cat]["label"].lower(),
                             "" if ttl else "  (без заголовка)")
        d.text((x, y + chh + 22), cap, font=brand.font("regular", 23), fill=MUTED, anchor="lm")
    return im


def main():
    p = argparse.ArgumentParser(description="Шаблонные обложки постов")
    p.add_argument("--city", choices=list(CITIES), default="magnitogorsk")
    p.add_argument("--category", choices=list(CATEGORIES), default="dtp")
    p.add_argument("--district", default="")
    p.add_argument("--title", default="")
    p.add_argument("--handle", default=None, help="по умолчанию — @юзернейм города из brand.py")
    p.add_argument("--avatar-style", choices=avatars.STYLES, default="a")
    p.add_argument("--out")
    p.add_argument("--sheet", action="store_true")
    p.add_argument("--all", action="store_true")
    a = p.parse_args()

    root = os.path.join(brand.BRAND_DIR, "out", "covers")
    os.makedirs(root, exist_ok=True)

    if a.sheet or a.all:
        sheet().save(os.path.join(root, "CONTACT-SHEET-covers.png"))
        print("  out/covers/CONTACT-SHEET-covers.png")
    if a.all:
        for ck in CITIES:
            for cat in CATEGORIES:
                f = os.path.join(root, "%s_%s.png" % (ck, cat))
                cover(ck, cat, "Ленинский район").save(f)
                print("  " + os.path.relpath(f, brand.BRAND_DIR))
    if a.sheet or a.all:
        return

    out = a.out or os.path.join(root, "%s_%s.png" % (a.city, a.category))
    cover(a.city, a.category, a.district, a.title, a.handle, a.avatar_style).save(out)
    print("  " + out)


if __name__ == "__main__":
    main()
