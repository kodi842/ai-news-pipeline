"""Клип-фильтр (VIDEO-SPEC, жёсткое правило 3).

«Без жести в клипы: площадки банят шок-контент жёстче ТГ. В клипы идут только
safety=safe драфты, и дополнительно режутся кровь/тела/паника — отдельный
клип-фильтр строже канального.»

Смысл тестов — доказать именно СТРОГОСТЬ: всё, что фильтр отклоняет, канальный
safety-фильтр пропускает. Иначе «строже канального» осталось бы словами
в комментарии.
"""
import pytest

from tgtraff.clips import clipfilter
from tgtraff.safety import rules as channel_rules

CITY = "magnitogorsk"


def draft(title="Хук", body="Тело новости", verdict="safe", category="dtp",
          flags=None, status="published"):
    """Минимальный драфт в виде словаря — фильтру нужны только эти поля."""
    return {"id": 1, "city": CITY, "title": title, "body": body,
            "safety_verdict": verdict, "category": category,
            "flags": flags, "status": status}


# ---------- вердикт safety ----------

def test_safe_draft_passes():
    assert clipfilter.check(draft()).ok


@pytest.mark.parametrize("verdict", ["manual_only", "reject", None])
def test_only_safe_verdict_goes_to_clips(verdict):
    """В канал manual_only выходит по кнопке модератора, в клип — никогда."""
    verdict_result = clipfilter.check(draft(verdict=verdict))
    assert not verdict_result.ok
    assert "safety" in " ".join(verdict_result.reasons)


# ---------- строже канального: жесть ----------

GORE = [
    "На асфальте осталась кровь пострадавшего",
    "Тело погибшего накрыли простынёй до приезда следователей",
    "Труп мужчины нашли у гаражей",
    "В торговом центре началась паника, люди бежали к выходу",
    "В давке у выхода пострадали трое",
    "Водителя зажало в искорёженном салоне",
    "Погиб ребёнок пяти лет",
    "Останки нашли на месте пожара",
]


@pytest.mark.parametrize("text", GORE)
def test_gore_is_cut_from_clips(text):
    verdict = clipfilter.check(draft(body=text))
    assert not verdict.ok, f"клип-фильтр пропустил жесть: {text!r}"


@pytest.mark.parametrize("text", GORE)
def test_the_same_text_is_publishable_in_the_channel(text):
    """Доказательство строгости: канальный фильтр эти же тексты пропускает
    (ЧП-канал пишет о тяжёлом — иначе это не канал происшествий)."""
    assert channel_rules.check_text(text).verdict == channel_rules.SAFE


def test_gore_in_the_hook_is_caught_too():
    """Плашка хука видна первой — жесть в ней тем более недопустима."""
    assert not clipfilter.check(draft(title="Кровь на переходе", body="Обычный текст")).ok


ORDINARY = [
    "Автобус № 21 столкнулся с фурой у вокзала, движение перекрыто",
    "Горел расселённый дом на Наседкина, обошлось без пострадавших",
    "Воду в Зашекснинском обещают дать к пятнице",
    "Водитель иномарки погиб в ДТП на Ленина",       # факт гибели — не жесть
]


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_incidents_still_make_clips(text):
    """Фильтр не имеет права выкосить саму ленту ЧП: ДТП и пожары — фавориты
    спеки, и факт гибели в тексте клип не отменяет (жесть — это кадры и
    подробности, а не сам факт)."""
    assert clipfilter.check(draft(body=text)).ok, f"фильтр съел обычное ЧП: {text!r}"


# ---------- флаги сомнения ----------

def test_draft_without_facts_does_not_go_to_clips():
    """Сценарию клипа нужны 2-3 строки фактов. Драфт с пометкой «фактуры нет»
    дать их не может — клип вышел бы пустой плашкой."""
    verdict = clipfilter.check(draft(flags='["low_facts"]'))
    assert not verdict.ok
    assert "low_facts" in " ".join(verdict.reasons)


@pytest.mark.parametrize("flag", ["unsupported_facts", "media_unverified"])
def test_warning_flags_do_not_block_by_themselves(flag):
    """`unsupported_facts` — предупреждение автосверки, и оно НЕ запрещает клип:
    драфт с ним уже вышел в канал по кнопке человека, а сам сценарий клипа
    сверяется с текстом драфта отдельно (script.py). Пометка едет в карточку.
    `media_unverified` — вопрос выбора медиа, а не текста."""
    assert clipfilter.check(draft(flags=f'["{flag}"]')).ok


# ---------- категории ----------

def test_blocked_category_is_configurable():
    assert not clipfilter.check(draft(category="crime"), blocked=("crime",)).ok
    assert clipfilter.check(draft(category="crime")).ok


def test_reasons_are_human_readable():
    verdict = clipfilter.check(draft(verdict="manual_only", body="Труп у гаражей"))
    assert len(verdict.reasons) >= 2
    assert all(isinstance(r, str) and r for r in verdict.reasons)
