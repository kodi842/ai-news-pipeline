"""Сценарий клипа (VIDEO-SPEC): хук ≤6 слов, 2-3 строки фактов, CTA-концовка.

Главное, что здесь проверяется, — правила фактов. Сценарий пишется по УЖЕ
проверенному драфту, поэтому любое новое число или имя собственное в хуке и
субтитрах — выдумка, и её надо поймать до того, как клип уедет на площадку
(«Правила фактов — те же 12 железных из стайлгайда»).
"""
import pytest

from tgtraff.clips.media import ClipMedia
from tgtraff.clips.script import ClipScriptWriter, ScriptError
from tgtraff.clips.settings import ClipSettings
from tgtraff.llm.client import LlmError


class StubClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def ask_json(self, system, user, **kw):
        self.calls.append({"system": system, "user": user, **kw})
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply

        class _R:
            model = "stub"
            cost_usd = 0.0008
        return reply, _R()


DRAFT = {"id": 7, "city": "magnitogorsk", "category": "dtp", "district": "Ленинский",
         "title": "Автобус № 21 столкнулся с фурой у вокзала",
         "body": "ДТП произошло около восьми утра на Ленина. Пострадал водитель автобуса, "
                 "его увезли в больницу. Движение по проспекту перекрыто."}

VIDEO = ClipMedia(mode="video", kind="video", files=["/m/a.mp4"], media_ids=[1],
                  source_key="example_channel_5", reason="clean", duration=27.0, has_audio=True)
PHOTOS = ClipMedia(mode="photos", kind="photo", files=["/m/1.jpg", "/m/2.jpg"],
                   media_ids=[1, 2], source_key="example_channel_5", reason="clean")

GOOD = {"hook": "Автобус смял фуру",
        "lines": ["Утром на Ленина столкнулись автобус № 21 и фура",
                  "Водитель автобуса в больнице",
                  "Движение по проспекту перекрыто"],
        "cta": "Подробности — в нашем канале"}


def writer(client, settings=None, **kw):
    return ClipScriptWriter(client, settings or ClipSettings(), **kw)


# ---------- разбор ответа ----------

def test_script_is_parsed():
    script = writer(StubClient(GOOD)).write(DRAFT, VIDEO, city_name="Магнитогорск",
                                            username="example_channel_4")
    assert script.hook == "Автобус смял фуру"
    assert len(script.lines) == 3
    assert script.cost_usd == pytest.approx(0.0008)


def test_cta_carries_our_username_from_config_not_from_the_model():
    """Юзернейм канала модель придумывать не должна — он приходит из конфига."""
    reply = dict(GOOD, cta="Подписывайтесь на @левый_канал")
    script = writer(StubClient(reply)).write(DRAFT, VIDEO, city_name="Магнитогорск",
                                            username="example_channel_4")
    assert "@example_channel_4" in script.cta_text
    assert "левый_канал" not in script.cta_text


def test_empty_hook_is_an_error():
    with pytest.raises(ScriptError):
        writer(StubClient(dict(GOOD, hook="  "))).write(DRAFT, VIDEO)


def test_no_lines_is_an_error():
    with pytest.raises(ScriptError):
        writer(StubClient(dict(GOOD, lines=[]))).write(DRAFT, VIDEO)


def test_broken_model_error_is_not_swallowed():
    with pytest.raises(LlmError):
        writer(StubClient(LlmError("нет связи"))).write(DRAFT, VIDEO)


# ---------- хук ----------

def test_long_hook_is_regenerated_once():
    """Хук читают за 1.5 секунды: длинный переспрашиваем, а не режем молча —
    обрезанная фраза теряет смысл."""
    long_hook = dict(GOOD, hook="Автобус номер двадцать один столкнулся с большой фурой у вокзала")
    client = StubClient(long_hook, GOOD)
    script = writer(client).write(DRAFT, VIDEO)
    assert len(client.calls) == 2
    assert script.hook == "Автобус смял фуру"


def test_stubbornly_long_hook_is_kept_but_flagged():
    """Модель дважды дала длинный хук: клип не теряем, но карточка скажет
    модератору, что хук стоит перегенерить."""
    long_hook = dict(GOOD, hook="Автобус номер двадцать один столкнулся с фурой возле вокзала")
    script = writer(StubClient(long_hook, long_hook)).write(DRAFT, VIDEO)
    assert "hook_long" in script.flags


def test_hook_loses_trailing_dot_and_caps_lock():
    script = writer(StubClient(dict(GOOD, hook="АВТОБУС СМЯЛ ФУРУ."))).write(DRAFT, VIDEO)
    assert script.hook == "Автобус смял фуру"


# ---------- факты ----------

def test_invented_number_is_regenerated():
    """В драфте нет «трёх пострадавших» — значит, в клипе их быть не может."""
    invented = dict(GOOD, lines=["Пострадали три человека", "Движение перекрыто"])
    client = StubClient(invented, GOOD)
    script = writer(client).write(DRAFT, VIDEO)
    assert len(client.calls) == 2
    assert script.lines == GOOD["lines"]


def test_invented_fact_that_survives_regeneration_is_flagged():
    invented = dict(GOOD, lines=["Пострадали три человека", "Движение перекрыто"])
    script = writer(StubClient(invented, invented)).write(DRAFT, VIDEO)
    assert "unsupported_facts" in script.flags
    assert script.unsupported


def test_facts_from_the_draft_are_not_flagged():
    script = writer(StubClient(GOOD)).write(DRAFT, VIDEO)
    assert script.flags == []
    assert script.unsupported == []


# ---------- строки субтитров ----------

def test_lines_are_capped_by_count():
    many = dict(GOOD, lines=["Раз", "Два", "Три", "Четыре", "Пять"])
    script = writer(StubClient(many), ClipSettings()).write(DRAFT, VIDEO)
    assert len(script.lines) <= 3


def test_long_line_is_cut_on_a_word_boundary():
    """Замечено на приёмке: жёсткая обрезка по счётчику знаков резала слово
    пополам («участвовали люди с инвалидностью всех категорий и люди без
    ограничений по здоров»). Это и на плашке уродство, и озвучка читает обрубок."""
    long_line = ("В турнире участвовали люди с инвалидностью всех категорий и люди "
                 "без ограничений по здоровью, приехали из шести городов области")
    script = writer(StubClient(dict(GOOD, lines=[long_line]))).write(DRAFT, VIDEO)
    line = script.lines[0]
    assert len(line) <= 120
    assert not line.rstrip("…").endswith(" ")
    assert line.endswith("…")
    # последнее слово — целое
    assert long_line.startswith(line.rstrip("…"))
    assert line.rstrip("…").split()[-1] in long_line.split()


def test_short_line_is_not_touched():
    script = writer(StubClient(dict(GOOD, lines=["Движение перекрыто"]))).write(DRAFT, VIDEO)
    assert script.lines == ["Движение перекрыто"]


def test_empty_lines_are_dropped():
    script = writer(StubClient(dict(GOOD, lines=["Движение перекрыто", "", "   "]))).write(
        DRAFT, VIDEO)
    assert script.lines == ["Движение перекрыто"]


# ---------- промпт ----------

def test_prompt_reuses_the_channel_styleguide():
    """Спека: «промпт переиспользует style.md» — правила фактов не должны
    расходиться между постом и клипом."""
    client = StubClient(GOOD)
    writer(client).write(DRAFT, VIDEO)
    system = client.calls[0]["system"]
    assert "Железные правила" in system
    assert "Никаких выдуманных фактов" in system


def test_prompt_says_which_mode_is_being_built():
    """В режиме фото сценарий ЗВУЧИТ (его читает озвучка), в режиме видео —
    только подписывает кадры. Это разные тексты."""
    client = StubClient(GOOD)
    writer(client).write(DRAFT, PHOTOS)
    assert "озвуч" in client.calls[0]["user"].lower()

    client = StubClient(GOOD)
    writer(client).write(DRAFT, VIDEO)
    assert "субтитр" in client.calls[0]["user"].lower()


def test_script_uses_its_own_purpose_for_accounting():
    client = StubClient(GOOD)
    writer(client).write(DRAFT, VIDEO)
    assert client.calls[0]["purpose"] == "clip_script"


def test_speech_text_is_what_the_voice_reads():
    """В режиме фото субтитры = озвучка (приёмка V1 проверяет их совпадение),
    поэтому текст один и берётся из одного места."""
    script = writer(StubClient(GOOD)).write(DRAFT, PHOTOS, city_name="Магнитогорск",
                                            username="example_channel_4")
    assert script.speech_text.startswith("Автобус смял фуру")
    for line in script.lines:
        assert line in script.speech_text
