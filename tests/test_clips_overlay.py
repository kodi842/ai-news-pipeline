"""Плашки клипа (VIDEO-SPEC): хук сверху, бейдж города, субтитры, аутро с CTA.

Рисуются Pillow'ом из бренд-пакета R0 — тем же, что рисует аватарки и обложки
постов. Никакой генерации нейросетями (жёсткое правило 4 спеки): всё, что
появляется в кадре поверх реального медиа, — типографика и геометрия.

Проверяются вещи, которые ломаются молча: размер кадра, прозрачность (иначе
плашка закроет само видео), место субтитров (низ кадра занимает интерфейс
площадки) и то, что текст на плашку реально попал.
"""
import pytest

from PIL import Image

from tgtraff.clips import overlay
from tgtraff.clips.settings import ClipSettings

CITY = "magnitogorsk"
SET = ClipSettings()


def opaque_rows(path):
    """Строки кадра, где есть непрозрачные пиксели (по альфе)."""
    im = Image.open(path).convert("RGBA")
    alpha = im.split()[3]
    return [y for y in range(im.height) if alpha.crop((0, y, im.width, y + 1)).getextrema()[1] > 8]


def test_hook_plate_is_vertical_frame(tmp_path):
    path = overlay.hook_plate(CITY, "Автобус смял столб", out_dir=tmp_path, settings=SET)
    with Image.open(path) as im:
        assert im.size == (SET.width, SET.height)
        assert im.mode == "RGBA"


def test_hook_plate_leaves_the_middle_of_the_frame_visible(tmp_path):
    """Плашка — накладка на живое видео: центр кадра она закрывать не имеет права."""
    path = overlay.hook_plate(CITY, "Горит рынок", out_dir=tmp_path, settings=SET)
    rows = opaque_rows(path)
    middle = range(int(SET.height * 0.40), int(SET.height * 0.60))
    assert not (set(rows) & set(middle))
    assert rows, "плашка вообще ничего не нарисовала"


def test_hook_plate_stays_in_the_top_third(tmp_path):
    path = overlay.hook_plate(CITY, "Горит рынок", out_dir=tmp_path, settings=SET)
    assert max(opaque_rows(path)) < SET.height * 0.40


def test_hook_plate_uses_the_city_accent(tmp_path):
    """Три города — три цвета: клип узнаётся раньше, чем читается текст."""
    mgn = overlay.hook_plate("magnitogorsk", "Горит рынок", out_dir=tmp_path, settings=SET)
    che = overlay.hook_plate("cherepovets", "Горит рынок", out_dir=tmp_path, settings=SET)
    assert Image.open(mgn).tobytes() != Image.open(che).tobytes()


def test_long_hook_wraps_and_does_not_crash(tmp_path):
    long_hook = "Автобус номер двадцать один столкнулся с фурой у железнодорожного вокзала"
    path = overlay.hook_plate(CITY, long_hook, out_dir=tmp_path, settings=SET)
    assert max(opaque_rows(path)) < SET.height * 0.45


def test_plate_is_reused_for_the_same_text(tmp_path):
    """Рендер идёт минутами — второй раз ту же плашку не рисуем."""
    first = overlay.hook_plate(CITY, "Горит рынок", out_dir=tmp_path, settings=SET)
    stamp = first.stat().st_mtime_ns
    second = overlay.hook_plate(CITY, "Горит рынок", out_dir=tmp_path, settings=SET)
    assert first == second
    assert second.stat().st_mtime_ns == stamp


def test_different_text_gives_different_file(tmp_path):
    a = overlay.hook_plate(CITY, "Горит рынок", out_dir=tmp_path, settings=SET)
    b = overlay.hook_plate(CITY, "Затопило двор", out_dir=tmp_path, settings=SET)
    assert a != b


# ---------- субтитры ----------

def test_subtitle_plate_sits_above_the_platform_interface(tmp_path):
    """Низ кадра занимают кнопки ВК/Rutube — субтитры туда ставить нельзя,
    иначе их не прочитать ни в одном клипе."""
    path = overlay.subtitle_plate(CITY, "Движение по проспекту перекрыто",
                                  out_dir=tmp_path, settings=SET)
    rows = opaque_rows(path)
    assert min(rows) > SET.height * 0.45
    assert max(rows) < SET.height * 0.88


def test_subtitle_plate_wraps_long_text(tmp_path):
    long_line = ("Утром на пересечении Ленина и Комсомольской столкнулись "
                 "пассажирский автобус и грузовая фура")
    path = overlay.subtitle_plate(CITY, long_line, out_dir=tmp_path, settings=SET)
    rows = opaque_rows(path)
    short = opaque_rows(overlay.subtitle_plate(CITY, "Коротко", out_dir=tmp_path, settings=SET))
    assert len(rows) > len(short), "длинный текст не перенёсся на вторую строку"


def test_empty_subtitle_gives_nothing(tmp_path):
    assert overlay.subtitle_plate(CITY, "   ", out_dir=tmp_path, settings=SET) is None


# ---------- бейдж ----------

def test_badge_marks_the_city_without_covering_the_frame(tmp_path):
    path = overlay.badge_plate(CITY, out_dir=tmp_path, settings=SET)
    rows = opaque_rows(path)
    assert max(rows) < SET.height * 0.20
    assert min(rows) >= 0


# ---------- аутро ----------

def test_outro_is_a_full_opaque_frame_with_cta(tmp_path):
    path = overlay.outro_frame(CITY, "Подробности — в нашем канале\n@example_channel_4",
                               out_dir=tmp_path, settings=SET)
    with Image.open(path) as im:
        assert im.size == (SET.width, SET.height)
        # Аутро — отдельный кадр, а не накладка: он закрывает весь экран.
        assert len(opaque_rows(path)) > SET.height * 0.9


def test_outro_shows_the_username(tmp_path):
    """Юзернейм на аутро — единственный способ найти канал после клипа."""
    with_handle = overlay.outro_frame(CITY, "Подробности — в канале\n@example_channel_4",
                                      out_dir=tmp_path, settings=SET)
    without = overlay.outro_frame(CITY, "Подробности — в канале", out_dir=tmp_path,
                                  settings=SET)
    assert Image.open(with_handle).tobytes() != Image.open(without).tobytes()


def test_unknown_city_does_not_crash(tmp_path):
    """Город из конфига может не совпасть с бренд-пакетом — клип важнее плашки."""
    path = overlay.hook_plate("kotlas", "Горит рынок", out_dir=tmp_path, settings=SET)
    assert path is not None and path.exists()


def test_badge_is_readable_on_a_light_photo(tmp_path):
    """Бейдж висит весь клип поверх ЧУЖОГО кадра, и кадр бывает светлым:
    белые буквы без подложки на снегу или на небе не читаются вовсе."""
    path = overlay.badge_plate(CITY, out_dir=tmp_path, settings=SET)
    im = Image.open(path).convert("RGBA")
    row = int(SET.height * (overlay.BADGE_TOP + 0.026))       # середина бейджа
    dark_backing = 0
    for x in range(0, im.width, 4):
        r, g, b, a = im.getpixel((x, row))
        if a > 100 and (r + g + b) / 3 < 90:
            dark_backing += 1
    assert dark_backing > 20, "под бейджем нет тёмной подложки — на светлом кадре он пропадёт"
