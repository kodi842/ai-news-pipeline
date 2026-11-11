"""Дневной дайджест в админ-бот (спека §9, фаза 6).

Дайджест — единственная сводка, которую владелец видит каждый день, поэтому
проверяется не «сообщение ушло», а что в нём: цифры гейта месяца 2 (подписчики,
прирост, охват), состояние конвейера и честные прочерки там, где данных нет.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tgtraff import drafts as drafts_store
from tgtraff.db import Database
from tgtraff.metrics import store
from tgtraff.metrics.digest import DailyDigest
from tgtraff.metrics.settings import MetricsSettings
from tgtraff.publisher.channels import ChannelsConfig, CityChannels, TelegramChannel, VkGroup
from tgtraff.publisher.settings import parse_slots

CITY, CHAT = "cherepovets", "-1001"
TZ = "Europe/Moscow"
# 22:30 по Москве — час дайджеста по умолчанию.
NOW = datetime(2026, 9, 5, 19, 30, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def channels():
    city = CityChannels(city=CITY, deep_link="che",
                        tg=TelegramChannel(chat_id=CHAT, username="example_channel_1",
                                           title="ЧП Череповец"),
                        vk=VkGroup(), slots=parse_slots(["18:00-22:00"]),
                        min_gap_minutes=20, max_gap_minutes=40, daily_limit=18)
    return ChannelsConfig(mode="test", ugc_bot="example_channel_3", cities={CITY: city})


class Sink:
    def __init__(self):
        self.sent = []

    async def __call__(self, text):
        self.sent.append(text)


def digest(db, sink=None, **kw):
    return DailyDigest(db, sink or Sink(), channels(),
                       MetricsSettings(**kw), city_names={CITY: "Череповец"},
                       city_tz={CITY: TZ})


def published(db, title, views=None, at=None, origin="news", city=CITY):
    did = drafts_store.save_draft(db, cluster_id=None, city=city, title=title, body="тело",
                                  origin=origin, status="published")
    db.conn.execute(
        """INSERT INTO published (draft_id, city, platform, channel, message_id,
                                  published_at, views)
           VALUES (?,?,'tg',?,?,?,?)""",
        (did, city, CHAT, str(1000 + did),
         (at or NOW - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"), views))
    return did


# ---- содержание ----

def test_digest_shows_posts_subscribers_and_growth(db):
    published(db, "Первая", views=800)
    published(db, "Вторая", views=1200)
    store.save_snapshot(db, CITY, CHAT, subscribers=1000, captured_at=NOW - timedelta(days=1))
    store.save_snapshot(db, CITY, CHAT, subscribers=1075, views_24h=2000, er=0.5,
                        posts_24h=2, captured_at=NOW)

    text = digest(db).build(now=NOW)

    assert "Череповец" in text
    assert "1 075" in text, "подписчики (разряды разделяются пробелом)"
    assert "+75" in text, "суточный прирост"
    assert "2" in text, "постов за день"


def test_digest_shows_the_days_top_posts(db):
    published(db, "Мелкая новость", views=10)
    published(db, "Главная новость дня", views=5000)

    text = digest(db, digest_top=1).build(now=NOW)

    assert "Главная новость дня" in text
    assert "Мелкая новость" not in text


def test_missing_metrics_are_shown_as_dashes_not_zeros(db):
    """Просмотры ещё не снимались — в дайджесте прочерк. Ноль здесь читался бы
    как «никто не читает», а это другое утверждение."""
    published(db, "Пост без замера")

    text = digest(db).build(now=NOW)

    assert "—" in text
    assert "ER 0" not in text


def test_digest_reports_the_moderation_queue(db):
    for i in range(3):
        drafts_store.save_draft(db, cluster_id=None, city=CITY, title=f"ждёт {i}", body="т")

    assert "3" in digest(db).build(now=NOW)


def test_digest_counts_ugc_of_the_day(db):
    drafts_store.save_draft(db, cluster_id=None, city=CITY, title="от читателя", body="т",
                            origin="ugc", flags=["ugc"])
    text = digest(db).build(now=NOW)
    assert "UGC" in text or "ugc" in text


def test_digest_shows_link_growth_and_cpf(db):
    link = store.ensure_link(db, CITY, CHAT, "vk_seeding", url="u", kind="paid")
    store.set_cost(db, link["id"], 6000)
    store.record_join(db, link["id"], "2026-09-05", count=200)

    text = digest(db).build(now=NOW)

    assert "vk_seeding" in text
    assert "30" in text, "CPF = 6000/200"


def test_digest_names_the_days_llm_bill(db):
    db.conn.execute(
        """INSERT INTO llm_usage (created_at, purpose, model, cost_usd)
           VALUES (?, 'rewrite', 'claude-haiku-4-5', 1.6)""",
        (NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),))

    assert "1.6" in digest(db).build(now=NOW)


def test_digest_warns_when_views_are_not_collected(db):
    """Молчащие метрики опаснее плохих: в дайджесте должно быть видно, что
    просмотры не снимаются, иначе гейт будет считаться по пустоте."""
    for i in range(5):
        published(db, f"Пост {i}")

    assert "просмотры" in digest(db).build(now=NOW).lower()


# ---- расписание ----

async def test_digest_is_sent_once_a_day_at_its_hour(db):
    published(db, "Новость", views=1)
    sink = Sink()
    d = digest(db, sink)

    assert await d.ensure(now=NOW.replace(hour=10)) is None, "не тот час"
    assert await d.ensure(now=NOW) is not None
    assert await d.ensure(now=NOW + timedelta(minutes=20)) is None, "уже отправляли"
    assert len(sink.sent) == 1


async def test_digest_can_be_switched_off(db):
    sink = Sink()
    assert await digest(db, sink, digest_enabled=False).ensure(now=NOW) is None
    assert not sink.sent


async def test_digest_is_sent_even_on_an_empty_day(db):
    """Пустой день — тоже новость: конвейер мог встать, и молчание дайджеста
    выглядело бы как «всё в порядке»."""
    sink = Sink()
    assert await digest(db, sink).ensure(now=NOW) is not None
    assert sink.sent


def test_digest_counts_drafts_that_went_stale_today(db):
    """§55: счётчик протухших — в дневной сводке; давно протухшие не считаются."""
    for i in range(2):
        did = drafts_store.save_draft(db, cluster_id=None, city=CITY, title=f"протух {i}", body="т")
        db.conn.execute("UPDATE drafts SET status='stale', decided_at=? WHERE id=?",
                        (store.iso(NOW - timedelta(hours=1)), did))
    old = drafts_store.save_draft(db, cluster_id=None, city=CITY, title="давно протух", body="т")
    db.conn.execute("UPDATE drafts SET status='stale', decided_at=? WHERE id=?",
                    (store.iso(NOW - timedelta(days=2)), old))

    assert "протухло за сутки: 2" in digest(db).build(now=NOW)


def test_digest_counts_posts_that_went_out_without_a_moderator(db):
    """§59: автопосты — отдельной строкой. Это единственная сводка, где владелец
    видит, сколько канал выпустил сам, и с чего начинать разбор инцидента."""
    for i in range(3):
        did = drafts_store.save_draft(db, cluster_id=None, city=CITY, title=f"авто {i}", body="т",
                                      status="approved")
        db.conn.execute("UPDATE drafts SET autopublished_at=? WHERE id=?",
                        (store.iso(NOW - timedelta(hours=2)), did))
    old = drafts_store.save_draft(db, cluster_id=None, city=CITY, title="вчерашний", body="т",
                                  status="published")
    db.conn.execute("UPDATE drafts SET autopublished_at=? WHERE id=?",
                    (store.iso(NOW - timedelta(days=2)), old))
    drafts_store.save_draft(db, cluster_id=None, city=CITY, title="руками", body="т",
                            status="approved")

    assert "автопостом за сутки: 3" in digest(db).build(now=NOW)
