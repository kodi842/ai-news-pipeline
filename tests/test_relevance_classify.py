"""Relevance-фильтр целиком: правила + дешёвый классификатор (спека §4).

Классификатор здесь подставной: проверяется не качество модели (это делает
приёмка на живой ленте), а логика — когда к модели вообще обращаются, что ей
показывают и что происходит с её ответом.
"""
import pytest

from tgtraff.config import SourceSpec
from tgtraff.db import Database
from tgtraff.llm.client import LlmError
from tgtraff.relevance.classify import RelevanceChecker
from tgtraff.relevance.settings import RelevanceSettings
from tgtraff.rewriter.material import collect_material

CITY = "magnitogorsk"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


class StubClient:
    """Отдаёт заранее заданные JSON-ответы и запоминает запросы."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def ask_json(self, system, user, **kw):
        self.prompts.append(user)
        reply = self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply

        class _R:
            model = "stub"
            cost_usd = 0.0001

            class usage:
                input_tokens = 10
                output_tokens = 5
                cache_read = 0
                cache_write = 0
        return reply, _R()


def material(db, text, source_key="example_channel_13", marked_as_ads=False):
    sid = db.upsert_source(SourceSpec(city=CITY, source_type="tg", key=source_key))
    raw_json = '{"marked_as_ads": true}' if marked_as_ads else None
    pid = db.insert_raw_post(source_id=sid, source_type="tg", external_id=str(abs(hash(text)) % 10**6),
                             published_at="2026-09-02T09:00:00+00:00", text=text,
                             raw_json=raw_json).id
    cur = db.conn.execute(
        "INSERT INTO clusters (city, status, lead_raw_post_id, size, first_seen_at, last_seen_at)"
        " VALUES (?, 'open', ?, 1, '2026-09-02T09:00:00+00:00', '2026-09-02T09:00:00+00:00')",
        (CITY, pid))
    db.conn.execute("INSERT INTO cluster_members (cluster_id, raw_post_id, reason) VALUES (?,?,'new')",
                    (cur.lastrowid, pid))
    return collect_material(db, cur.lastrowid, city_name="Магнитогорск")


NEWS = ("На перекрёстке Ленина и Гагарина столкнулись такси и грузовик. "
        "Пострадал водитель такси, его увезли в больницу №3, движение затруднено.")


def test_rules_decide_without_calling_the_model(db):
    client = StubClient({"verdict": "news"})
    checker = RelevanceChecker(client, RelevanceSettings())

    result = checker.check(material(db, "Скидки! erid:2Vtzqxkmj5d"))

    assert result.verdict == "ad"
    assert result.by == "rules"
    assert client.prompts == []          # платного запроса не было


def test_vk_ad_flag_is_read_from_raw_json(db):
    checker = RelevanceChecker(StubClient({"verdict": "news"}), RelevanceSettings())
    result = checker.check(material(db, "Отличное предложение месяца", marked_as_ads=True))
    assert result.verdict == "ad"
    assert result.reason == "marked_as_ads"


def test_model_is_asked_when_rules_are_unsure(db):
    client = StubClient({"verdict": "news", "reason": "городское ДТП"})
    result = RelevanceChecker(client, RelevanceSettings()).check(material(db, NEWS))

    assert result.verdict == "news"
    assert result.by == "llm"
    assert len(client.prompts) == 1
    assert "Магнитогорск" in client.prompts[0]
    assert "столкнулись такси" in client.prompts[0]


def test_model_can_drop_a_post_about_another_city(db):
    client = StubClient({"verdict": "off_topic", "reason": "новость про Челябинск"})
    text = ("Прокуратура начала проверку из-за лифта в Челябинске: надзорное ведомство "
            "отреагировало на публикацию СМИ о падении кабины с людьми.")
    result = RelevanceChecker(client, RelevanceSettings()).check(material(db, text))

    assert result.verdict == "off_topic"
    assert result.is_drop is True


def test_unknown_verdict_from_the_model_is_treated_as_news(db):
    """Мусор в ответе не должен молча выбрасывать новость из ленты."""
    client = StubClient({"verdict": "может быть"})
    result = RelevanceChecker(client, RelevanceSettings()).check(material(db, NEWS))
    assert result.verdict == "news"
    assert result.by == "fallback"


def test_model_failure_keeps_the_cluster_in_the_pipeline(db):
    client = StubClient(LlmError("API упал"))
    result = RelevanceChecker(client, RelevanceSettings()).check(material(db, NEWS))
    assert result.verdict == "news"
    assert result.by == "fallback"
    assert "API упал" in (result.error or "")


def test_classifier_can_be_switched_off(db):
    client = StubClient({"verdict": "off_topic"})
    checker = RelevanceChecker(client, RelevanceSettings(use_llm=False))
    result = checker.check(material(db, NEWS))
    assert result.verdict == "news"
    assert client.prompts == []
