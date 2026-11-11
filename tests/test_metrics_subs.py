"""Снимки канала (спека §9): подписчики, охват, ER.

Подписчиков отдаёт Bot API (`getChatMemberCount`) — бот и так админ канала.
Охват и ER считаются из уже снятых просмотров, поэтому здесь проверяется
арифметика и поведение на пустых данных: канал без просмотров не должен
рождать ER, деля на ноль.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tgtraff import drafts as drafts_store
from tgtraff.db import Database
from tgtraff.metrics import store
from tgtraff.metrics.settings import MetricsSettings
from tgtraff.metrics.subs import SubsCollector
from tgtraff.publisher.channels import ChannelsConfig, CityChannels, TelegramChannel, VkGroup
from tgtraff.publisher.settings import parse_slots

CITY = "cherepovets"
NOW = datetime(2026, 9, 5, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def channels(chat_id="-1001"):
    city = CityChannels(city=CITY, deep_link="che",
                        tg=TelegramChannel(chat_id=chat_id, username="example_channel_1",
                                           title="ЧП Череповец"),
                        vk=VkGroup(), slots=parse_slots(["18:00-22:00"]),
                        min_gap_minutes=20, max_gap_minutes=40, daily_limit=18)
    return ChannelsConfig(mode="test", ugc_bot="example_channel_3", cities={CITY: city})


class FakeBot:
    def __init__(self, count=1000, error=None):
        self.count, self.error = count, error
        self.asked = []

    async def get_chat_member_count(self, chat_id):
        self.asked.append(chat_id)
        if self.error:
            raise self.error
        return self.count


def published(db, views, at=None, city=CITY):
    did = drafts_store.save_draft(db, cluster_id=None, city=city, title="пост", body="тело",
                                  status="published")
    db.conn.execute(
        """INSERT INTO published (draft_id, city, platform, channel, message_id,
                                  published_at, views)
           VALUES (?,?,'tg','-1001',?,?,?)""",
        (did, city, str(1000 + did),
         (at or NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"), views))


async def test_snapshot_records_subscribers_and_reach(db):
    published(db, 500)
    published(db, 700)
    bot = FakeBot(count=1200)

    await SubsCollector(db, bot, channels(), MetricsSettings()).tick(now=NOW)

    row = store.last_snapshot(db, CITY)
    assert row["subscribers"] == 1200
    assert row["views_24h"] == 1200          # 500 + 700
    assert row["posts_24h"] == 2
    # ER канала — средний охват поста к подписчикам: (1200/2) / 1200 = 0.5
    assert row["er"] == pytest.approx(0.5)


async def test_er_is_empty_without_views(db):
    """Просмотров ещё нет — ER не выдумывается: пустое поле честнее нуля."""
    published(db, None)
    await SubsCollector(db, FakeBot(count=800), channels(), MetricsSettings()).tick(now=NOW)

    row = store.last_snapshot(db, CITY)
    assert row["subscribers"] == 800
    assert row["er"] is None


async def test_yesterdays_posts_are_out_of_the_reach_window(db):
    published(db, 999, at=NOW - timedelta(days=2))
    await SubsCollector(db, FakeBot(), channels(), MetricsSettings()).tick(now=NOW)

    assert store.last_snapshot(db, CITY)["views_24h"] == 0


async def test_city_without_a_channel_is_skipped(db):
    bot = FakeBot()
    await SubsCollector(db, bot, channels(chat_id=None), MetricsSettings()).tick(now=NOW)

    assert not bot.asked
    assert store.last_snapshot(db, CITY) is None


async def test_bot_failure_does_not_break_the_tick(db):
    alerts = []
    collector = SubsCollector(db, FakeBot(error=RuntimeError("бот не админ")), channels(),
                              MetricsSettings(), alerter=_Alerter(alerts))

    assert await collector.tick(now=NOW) == 0
    assert alerts


async def test_growth_is_measured_against_the_snapshot_a_day_ago(db):
    store.save_snapshot(db, CITY, "-1001", subscribers=1000,
                        captured_at=NOW - timedelta(days=1))
    await SubsCollector(db, FakeBot(count=1080), channels(), MetricsSettings()).tick(now=NOW)

    assert store.growth(db, CITY, NOW, hours=24) == 80


class _Alerter:
    def __init__(self, sink):
        self.sink = sink

    async def alert(self, message):
        self.sink.append(message)
        return True
