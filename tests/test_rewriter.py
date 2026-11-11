"""Rewriter (спека §5): кластер → хук + текст в стиле канала + категория + медиа.

Модель подставная: проверяется контракт вокруг неё — что уходит в промпт, как
разбирается ответ, что происходит при мусоре и, главное, ловушка на выдуманные
факты («Запрещено выдумывать факты, отсутствующие в кластере»).
"""
import pytest

from tgtraff.config import SourceSpec
from tgtraff.db import Database
from tgtraff.llm.client import LlmError
from tgtraff.rewriter.material import collect_material
from tgtraff.rewriter.media import MediaSettings
from tgtraff.rewriter.rewrite import Rewriter, unsupported_facts
from tgtraff.rewriter.settings import RewriterSettings

CITY = "cherepovets"

GOOD = {
    "title": "На Победы столкнулись такси и грузовик",
    "body": "Авария произошла у дома 45 около восьми утра. Пострадал водитель такси — "
            "его увезли в больницу. Движение по проспекту затруднено.",
    "category": "dtp",
    "district": "Зашекснинский",
    "urgency": "normal",
    "low_facts": False,
}


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


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
            cost_usd = 0.0009

            class usage:
                input_tokens = 1200
                output_tokens = 200
                cache_read = 1000
                cache_write = 0
        return reply, _R()


class NoWatermarks:
    def verdict(self, media):
        return "none", None


def material(db, *texts, media_path=None, source_keys=None):
    keys = source_keys or [f"src{i}" for i in range(len(texts))]
    ids = []
    for text, key in zip(texts, keys):
        sid = db.upsert_source(SourceSpec(city=CITY, source_type="tg", key=key))
        pid = db.insert_raw_post(source_id=sid, source_type="tg",
                                 external_id=str(abs(hash((key, text))) % 10**7),
                                 published_at="2026-09-02T08:00:00+00:00", text=text,
                                 has_media=1 if media_path else 0,
                                 media_state="pending" if media_path else "none").id
        if media_path:
            mid = db.add_media(pid, 0, "photo")
            db.conn.execute("UPDATE raw_media SET status='done', file_path=?, watermark='none'"
                            " WHERE id=?", (media_path, mid))
            media_path = None
        ids.append(pid)
    cur = db.conn.execute(
        "INSERT INTO clusters (city, status, lead_raw_post_id, size, first_seen_at, last_seen_at)"
        " VALUES (?, 'open', ?, ?, '2026-09-02T08:00:00+00:00','2026-09-02T08:00:00+00:00')",
        (CITY, ids[0], len(ids)))
    for pid in ids:
        db.conn.execute("INSERT INTO cluster_members (cluster_id, raw_post_id, reason)"
                        " VALUES (?,?,'new')", (cur.lastrowid, pid))
    return collect_material(db, cur.lastrowid, city_name="Череповец")


FACTS = ("На проспекте Победы у дома 45 около восьми утра столкнулись такси и грузовик. "
         "Пострадал водитель такси, его увезли в больницу. Движение затруднено. "
         "Зашекснинский район.")


def rewriter(client, **kw):
    return Rewriter(client, RewriterSettings(**kw), media_settings=MediaSettings())


# --- разбор ответа ----------------------------------------------------------

def test_draft_carries_hook_body_and_category(db):
    draft = rewriter(StubClient(GOOD)).rewrite(material(db, FACTS), NoWatermarks())
    assert draft.title == GOOD["title"]
    assert draft.body.startswith("Авария произошла")
    assert draft.category == "dtp"
    assert draft.district == "Зашекснинский"


def test_unknown_category_falls_back_to_other(db):
    draft = rewriter(StubClient({**GOOD, "category": "котики"})).rewrite(
        material(db, FACTS), NoWatermarks())
    assert draft.category == "other"


def test_empty_body_raises(db):
    with pytest.raises(LlmError):
        rewriter(StubClient({**GOOD, "body": ""})).rewrite(material(db, FACTS), NoWatermarks())


def test_model_failure_propagates(db):
    with pytest.raises(LlmError):
        rewriter(StubClient(LlmError("timeout"))).rewrite(material(db, FACTS), NoWatermarks())


# --- промпт -----------------------------------------------------------------

def test_prompt_contains_every_source_text(db):
    m = material(db, FACTS, "ДТП на Победы: пострадал таксист", source_keys=["a", "b"])
    client = StubClient(GOOD)
    rewriter(client).rewrite(m, NoWatermarks())
    user = client.calls[0]["user"]
    assert "столкнулись такси и грузовик" in user
    assert "пострадал таксист" in user.lower()


def test_system_prompt_holds_styleguide_and_is_the_same_for_every_city(db):
    """Стайлгайд и few-shot одинаковы у всех городов — иначе кэш не переиспользуется."""
    client = StubClient(GOOD)
    r = rewriter(client)
    r.rewrite(material(db, FACTS), NoWatermarks())
    r.rewrite(material(db, FACTS + " Ещё раз."), NoWatermarks())
    assert client.calls[0]["system"] == client.calls[1]["system"]
    assert "ЧП" in client.calls[0]["system"]
    # Фактура кластера в системный промпт не попадает — иначе кэш ломается на
    # каждой новости (примеры few-shot с названиями городов статичны и не в счёт).
    assert FACTS not in client.calls[0]["system"]


def test_city_goes_into_the_user_message(db):
    client = StubClient(GOOD)
    rewriter(client).rewrite(material(db, FACTS), NoWatermarks())
    assert "Череповец" in client.calls[0]["user"]


# --- запрет на выдуманные факты ---------------------------------------------

def test_invented_number_is_reported_as_unsupported():
    facts = "На Победы столкнулись такси и грузовик, пострадал водитель."
    assert unsupported_facts("Погибли 3 человека", [facts]) == ["3"]


def test_numbers_present_in_the_material_are_fine():
    facts = "У дома 45 около восьми утра столкнулись такси и грузовик."
    assert unsupported_facts("Авария у дома 45", [facts]) == []


def test_numbers_in_different_form_are_still_matched():
    facts = "Пострадали 12 человек, всех увезли в больницу №3."
    assert unsupported_facts("В больницу №3 доставили 12 человек", [facts]) == []


def test_invented_name_is_reported():
    facts = "На Победы столкнулись такси и грузовик, пострадал водитель."
    assert "Иванов" in unsupported_facts("Погиб водитель Иванов", [facts])


def test_draft_with_invented_facts_is_flagged_and_sent_to_manual(db):
    bad = {**GOOD, "body": "В аварии погибли 3 человека, среди них ребёнок."}
    draft = rewriter(StubClient(bad)).rewrite(material(db, FACTS), NoWatermarks())
    assert "unsupported_facts" in draft.flags
    assert draft.unsupported


def test_clean_draft_has_no_fact_flag(db):
    draft = rewriter(StubClient(GOOD)).rewrite(material(db, FACTS), NoWatermarks())
    assert "unsupported_facts" not in draft.flags


# --- low_facts ---------------------------------------------------------------

def test_low_facts_cluster_is_flagged(db):
    m = material(db, "В посте есть вложения:", media_path="/m/a.jpg")
    draft = rewriter(StubClient({**GOOD, "low_facts": True})).rewrite(m, NoWatermarks())
    assert "low_facts" in draft.flags


def test_model_can_flag_low_facts_itself(db):
    draft = rewriter(StubClient({**GOOD, "low_facts": True})).rewrite(
        material(db, FACTS), NoWatermarks())
    assert "low_facts" in draft.flags


# --- медиа ------------------------------------------------------------------

def test_media_is_chosen_by_the_cascade(db):
    m = material(db, FACTS, media_path="/m/clean.jpg")
    draft = rewriter(StubClient(GOOD)).rewrite(m, NoWatermarks())
    assert draft.media.path == "/m/clean.jpg"
    assert draft.media.reason == "clean"


def test_draft_without_media_asks_for_template_cover(db):
    draft = rewriter(StubClient(GOOD)).rewrite(material(db, FACTS), NoWatermarks())
    assert draft.media.kind == "template"


def test_word_in_another_case_is_not_an_invented_fact():
    """Живая регрессия: «на ДОЗе» в источнике и «в районе ДОЗа» в драфте."""
    facts = "На Ильинском шоссе и ДОЗе сразу на нескольких АЗС бензина сейчас нет."
    assert unsupported_facts("В районе ДОЗа заправки без бензина", [facts]) == []


def test_localised_city_in_a_regional_story_is_still_caught():
    facts = "Поставки бензина в регион восстановят после 10 сентября."
    assert "Череповце" in unsupported_facts("Бензин в Череповце вернётся", [facts])


def test_time_written_as_hh_mm_is_not_an_invented_number():
    """Живая регрессия: источник «с 22 часов», драфт «с 22:00» — тот же факт."""
    facts = "С 22 часов пятницы до 5 часов понедельника перекроют перекрёсток."
    assert unsupported_facts("С 22:00 пятницы до 05:00 понедельника", [facts]) == []


def test_invented_time_is_still_caught():
    """Живая регрессия драфта #54: времени сбора в материале не было."""
    facts = "Фанатский марш состоится 5 сентября. Начало матча в 17 часов."
    assert "15" in unsupported_facts("Болельщики соберутся в 15:15", [facts])


def test_time_with_a_dot_matches_the_same_time_with_a_colon():
    """Решение ревью §33: «Сбор в 15.15» у источника и «15:15» в драфте —
    одна и та же запись. Ровно на этом споткнулся драфт #54."""
    facts = "Сбор болельщиков в 15.15 у Дворца спорта, начало марша в 16.00."
    assert unsupported_facts("Болельщики собираются в 15:15", [facts]) == []


def test_leading_zero_is_not_a_different_number():
    """«09:00» в источнике и «9 часов» в драфте — то же время."""
    facts = "Перекрёсток перекроют с 09:00 до 18:00."
    assert unsupported_facts("Движение закроют с 9 часов", [facts]) == []


def test_date_written_with_dots_matches_the_date_in_words():
    facts = "Отключение запланировано на 03.09.2026, работы идут с утра."
    assert unsupported_facts("Отключение 3 сентября", [facts]) == []


def test_decimals_are_not_split_into_separate_numbers():
    """Нормализация трогает только время и даты: «1.5 млн» остаётся одним
    числом, и «5» из него фактом не становится."""
    facts = "Ремонт обойдётся в 1.5 млн рублей."
    assert unsupported_facts("Пострадали 5 человек", [facts]) == ["5"]
