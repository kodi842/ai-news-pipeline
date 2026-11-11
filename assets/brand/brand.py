"""Единый бренд-конфиг сетки «ЧП [Город]».

Один источник правды для генераторов аватарок (avatars.py) и шаблонных обложек
постов (covers.py). Модуль без побочных эффектов — его импортирует конвейер
(медиа-каскад, шаг 3: «своя шаблонная обложка»).

Зависимости: Pillow. Шрифты — см. fonts/README.md.
"""
from __future__ import annotations

import os
from PIL import ImageFont

BRAND_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_FONTS = os.path.join(BRAND_DIR, "fonts")

# ---------------------------------------------------------------- палитра ---

# База сетки. Графит вместо чистого чёрного: на тёмной теме ТГ чёрный сливается
# с фоном, графит держит форму круга.
INK = "#14171C"        # основной тёмный (фон аватарки, фон обложки)
INK_DEEP = "#0B0D10"   # тень/подложка под текст
PAPER = "#FFFFFF"
PAPER_DIM = "#EEF1F5"
MUTED = "#8A93A3"      # подписи, служебный текст

# Акцент города. Три максимально разведённых по тону цвета — в списке чатов
# каналы сетки различаются по цвету раньше, чем читается код города.
UGC_BOT = "example_channel_3"      # один бот на сеть, город приходит deep-link'ом ?start=<code>

CITIES = {
    "magnitogorsk": {
        "name": "Магнитогорск",
        "name_gen": "Магнитогорска",   # родительный падеж — для «Новости ...»
        "code": "МГН",                 # буквенный код города (читается с ~96 px)
        "region": "74",                # автомобильный код региона
        "accent": "#E63329",           # сигнальный красный (ММК, металл)
        "tz": "Asia/Yekaterinburg",
        "tg": "example_channel_4",   # выбрано в DECISIONS.md
        "vk": "example_channel_4",
    },
    "novokuznetsk": {
        "name": "Новокузнецк",
        "name_gen": "Новокузнецка",
        "code": "НВК",
        "region": "42",
        "accent": "#F5A623",           # янтарь (Кузбасс, огонь кузницы)
        "tz": "Asia/Novokuznetsk",
        "tg": "chp_novokuznetsk42",
        "vk": "chp_novokuznetsk42",
    },
    "cherepovets": {
        "name": "Череповец",
        "name_gen": "Череповца",
        "code": "ЧЕР",
        "region": "35",
        "accent": "#2E86DE",           # холодный синий (север, Северсталь)
        "tz": "Europe/Moscow",
        "tg": "example_channel_1",
        "vk": "example_channel_1",
    },
}

# Категории постов = категории rewriter'а из PROJECT-SPEC.md §5.
# Цвет категории — акцент обложки; акцент города уходит в мелкий шеврон-локап,
# чтобы две цветовые оси не конкурировали.
CATEGORIES = {
    "dtp":      {"label": "ДТП",        "color": "#E63329", "icon": "warning"},
    "fire":     {"label": "ПОЖАР",      "color": "#FF6A13", "icon": "flame"},
    "utility":  {"label": "КОММУНАЛКА", "color": "#2E86DE", "icon": "drop"},
    "weather":  {"label": "ПОГОДА",     "color": "#00BCD4", "icon": "cloud"},
    "city":     {"label": "ГОРОД",      "color": "#4CAF6E", "icon": "tower"},
    # Дорисованы по решению ревью §30: на живой ленте каждый четвёртый драфт
    # с шаблонной обложкой — криминал или прочее, и оба выходили на городской.
    "crime":    {"label": "КРИМИНАЛ",   "color": "#9B6BE0", "icon": "shield"},
    "other":    {"label": "ПРОИСШЕСТВИЕ", "color": "#94A3B8", "icon": "info"},
}

# ----------------------------------------------------------------- шрифты ---

# PT Sans (ParaType) — кириллица родная, лицензия свободная, на Linux ставится
# пакетом `fonts-paratype`. Порядок кандидатов: вендор в репозитории → macOS →
# Linux → универсальный fallback.
_FONT_CANDIDATES = {
    # узкий гротеск: крупные заголовки, «ЧП», названия категорий
    "display": [
        (os.path.join(VENDOR_FONTS, "PTSansNarrow-Bold.ttf"), 0),
        ("/System/Library/Fonts/Supplemental/PTSans.ttc", 2),
        ("/usr/share/fonts/truetype/paratype-pt-sans/PTN77F.ttf", 0),
        ("/usr/share/fonts/truetype/paratype/PTN77F.ttf", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
    ],
    # обычный гротеск, жирный: подзаголовки, локап, чипы
    "bold": [
        (os.path.join(VENDOR_FONTS, "PTSans-Bold.ttf"), 0),
        ("/System/Library/Fonts/Supplemental/PTSans.ttc", 7),
        ("/usr/share/fonts/truetype/paratype-pt-sans/PTS75F.ttf", 0),
        ("/usr/share/fonts/truetype/paratype/PTS75F.ttf", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
    ],
    # обычный гротеск: служебные подписи
    "regular": [
        (os.path.join(VENDOR_FONTS, "PTSans-Regular.ttf"), 0),
        ("/System/Library/Fonts/Supplemental/PTSans.ttc", 0),
        ("/usr/share/fonts/truetype/paratype-pt-sans/PTS55F.ttf", 0),
        ("/usr/share/fonts/truetype/paratype/PTS55F.ttf", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
    ],
}

_resolved: dict = {}


def _resolve(role: str):
    if role in _resolved:
        return _resolved[role]
    for path, index in _FONT_CANDIDATES[role]:
        if os.path.exists(path):
            try:
                ImageFont.truetype(path, 24, index=index)
            except OSError:
                continue
            _resolved[role] = (path, index)
            return _resolved[role]
    raise RuntimeError(
        "Не найден шрифт для роли '%s'. Положите PT Sans в %s "
        "(см. fonts/README.md) или установите `fonts-paratype`." % (role, VENDOR_FONTS)
    )


def font(role: str, size: int) -> ImageFont.FreeTypeFont:
    """Шрифт по роли: display | bold | regular."""
    path, index = _resolve(role)
    return ImageFont.truetype(path, int(size), index=index)


def font_report() -> str:
    return "\n".join(
        "%-8s -> %s [%d]" % (r, _resolve(r)[0], _resolve(r)[1]) for r in _FONT_CANDIDATES
    )


# ------------------------------------------------------------ примитивы -----

def hex_rgb(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def text_width(f: ImageFont.FreeTypeFont, text: str, tracking_px: float = 0.0) -> float:
    """Ширина по метрикам (сумма advance + трекинг)."""
    if not text:
        return 0.0
    return sum(f.getlength(ch) for ch in text) + tracking_px * (len(text) - 1)


def ink_box(f: ImageFont.FreeTypeFont, text: str, tracking_px: float = 0.0):
    """Габарит «чернил» строки относительно точки отрисовки с anchor='la'.

    Возвращает (x0, y0, x1, y1). Нужен, чтобы центрировать заголовок по реальным
    буквам, а не по метрикам шрифта: у капса метрический центр заметно ниже
    оптического, и знак уезжает вниз.
    """
    x = 0.0
    box = None
    for ch in text:
        b = f.getbbox(ch, anchor="la")
        if b and (b[2] - b[0]) > 0:
            cur = (x + b[0], b[1], x + b[2], b[3])
            box = cur if box is None else (min(box[0], cur[0]), min(box[1], cur[1]),
                                           max(box[2], cur[2]), max(box[3], cur[3]))
        x += f.getlength(ch) + tracking_px
    return box or (0.0, 0.0, 0.0, 0.0)


def fit_font(role: str, text: str, max_width: float, start: int,
             max_height: float = None, min_size: int = 8, tracking: float = 0.0):
    """Подобрать кегль под ограничения по ширине и (опционально) высоте чернил.

    tracking задаётся в долях кегля (em), как в типографике.
    """
    size = int(start)
    while size > min_size:
        f = font(role, size)
        b = ink_box(f, text, tracking * size)
        if (b[2] - b[0]) <= max_width and (max_height is None or (b[3] - b[1]) <= max_height):
            return f
        size -= 1
    return font(role, min_size)


def draw_block(draw, center, text, f, fill, tracking_px: float = 0.0,
               stroke_width: int = 0):
    """Нарисовать строку так, чтобы центр её чернил попал в точку center."""
    b = ink_box(f, text, tracking_px)
    x = center[0] - (b[0] + b[2]) / 2.0
    y = center[1] - (b[1] + b[3]) / 2.0
    for ch in text:
        draw.text((x, y), ch, font=f, fill=fill, anchor="la",
                  stroke_width=stroke_width, stroke_fill=fill)
        x += f.getlength(ch) + tracking_px
    return b[2] - b[0]


def chord(diameter: float, y: float, inset: float = 0.0) -> float:
    """Ширина круга диаметра `diameter` на высоте y (для вписывания в аватарку)."""
    r = diameter / 2.0 - inset
    dy = abs(y - diameter / 2.0)
    if dy >= r:
        return 0.0
    return 2.0 * (r * r - dy * dy) ** 0.5
