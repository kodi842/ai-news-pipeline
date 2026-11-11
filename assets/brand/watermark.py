#!/usr/bin/env python3
"""Наш знак на собственном эксклюзиве (PROJECT-SPEC.md §5, последняя строка).

Ставится ТОЛЬКО на UGC — на то, что прислал читатель и чего больше нигде нет.
Чужие вотермарки не трогаются никогда: удаление/кроп/затирание запрещены
(ст. 1300 ГК РФ), и такой функциональности в конвейере нет.

Плашка в правом нижнем углу: графит с прозрачностью, белый хэндл канала —
тот же приём «плашка», что у аватарок и обложек R0.

Пример:
  python3 watermark.py --city cherepovets --in ugc.jpg --out stamped.jpg
"""
from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw

import brand
from brand import CITIES, INK_DEEP, PAPER

# Доли от высоты картинки: знак не должен спорить с содержимым кадра.
PLATE_HEIGHT = 0.062
MARGIN = 0.028
PAD_X = 0.55            # от высоты плашки
OPACITY = 190


def stamp(image: Image.Image, city_key: str, handle: str | None = None) -> Image.Image:
    """Возвращает копию кадра с нашей плашкой в правом нижнем углу."""
    city = CITIES[city_key]
    text = handle or ("@" + city["tg"])
    im = image.convert("RGB")
    W, H = im.size

    plate_h = max(22, int(H * PLATE_HEIGHT))
    font = brand.fit_font("bold", text, W * 0.42, start=int(plate_h * 0.56),
                          max_height=plate_h * 0.52)
    pad = int(plate_h * PAD_X)
    plate_w = int(brand.text_width(font, text) + pad * 2)
    margin = int(H * MARGIN)
    x1, y1 = W - margin, H - margin
    x0, y0 = x1 - plate_w, y1 - plate_h

    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle([x0, y0, x1, y1], radius=int(plate_h * 0.26),
                        fill=brand.hex_rgb(INK_DEEP) + (OPACITY,))
    d.rectangle([x0, y0, x0 + max(3, int(plate_h * 0.10)), y1],
                fill=brand.hex_rgb(city["accent"]) + (255,))
    d.text(((x0 + x1) / 2 + int(plate_h * 0.05), (y0 + y1) / 2), text, font=font,
           fill=brand.hex_rgb(PAPER) + (255,), anchor="mm")
    return Image.alpha_composite(im.convert("RGBA"), layer).convert("RGB")


def main():
    p = argparse.ArgumentParser(description="Наш вотермарк на UGC-эксклюзив")
    p.add_argument("--city", choices=list(CITIES), required=True)
    p.add_argument("--in", dest="src", required=True)
    p.add_argument("--out", dest="dst")
    a = p.parse_args()
    out = a.dst or os.path.join(brand.BRAND_DIR, "out", "ugc_stamped.jpg")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    stamp(Image.open(a.src), a.city).save(out, quality=92)
    print("  " + out)


if __name__ == "__main__":
    main()
