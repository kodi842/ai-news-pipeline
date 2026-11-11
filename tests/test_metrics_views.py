"""Съём просмотров собственных постов (спека §9, QUESTIONS §39).

Bot API просмотры не отдаёт — их читает юзербот. Здесь проверяется всё, что
не зависит от Telegram: кого спрашиваем, что записываем и как ведёт себя
конвейер, когда юзербота в канал не пустили.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tgtraff import drafts as drafts_store
from tgtraff.db import Database
from tgtraff.metrics import store
from tgtraff.metrics.settings import MetricsSettings
from tgtraff.metrics.views import ViewsCollector, ViewsSnapshot

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
CHANNEL = "-1001234567890"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


class FakeReader:
    """Двойник юзербота: отдаёт просмотры и помнит, о чём его спрашивали.

    `missing` — сообщения, которых в канале уже нет (пост удалили руками)."""

    def __init__(self, views=None, error=None, missing=()):
        self.views_map = views or {}
        self.error = error
        self.missing = set(missing)
        self.calls = []

    async def views(self, channel, message_ids):
        self.calls.append((str(channel), tuple(message_ids)))
        if self.error:
            raise self.error
        return ViewsSnapshot(
            views={mid: self.views_map[mid] for mid in message_ids if mid in self.views_map},
            missing={mid for mid in message_ids if mid in self.missing})


def publish(db, message_id: int, city="cherepovets", published_at=None, title="пост",
            origin="news", views=None):
    draft_id = drafts_store.save_draft(db, cluster_id=None, city=city, title=title,
                                       body="тело", origin=origin, status="published")
    db.conn.execute(
        """INSERT INTO published (draft_id, city, platform, channel, message_id, published_at, views)
           VALUES (?,?,'tg',?,?,?,?)""",
        (draft_id, city, CHANNEL, str(message_id),
         (published_at or NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), views))
    return draft_id


async def test_views_are_saved_to_published(db):
    publish(db, 10)
    publish(db, 11)
    reader = FakeReader({10: 1200, 11: 340})

    saved = await ViewsCollector(db, reader, MetricsSettings()).tick(now=NOW)

    assert saved == 2
    rows = db.conn.execute("SELECT message_id, views, views_at FROM published "
                           "ORDER BY message_id").fetchall()
    assert [(r["message_id"], r["views"]) for r in rows] == [("10", 1200), ("11", 340)]
    assert all(r["views_at"] for r in rows), "время замера должно записываться"


async def test_one_request_per_channel(db):
    """Пятнадцать постов одного канала — один запрос, а не пятнадцать:
    юзербот работает на живом аккаунте, лишние запросы ему ни к чему."""
    for mid in range(1, 16):
        publish(db, mid)
    reader = FakeReader({mid: mid * 10 for mid in range(1, 16)})

    await ViewsCollector(db, reader, MetricsSettings()).tick(now=NOW)

    assert len(reader.calls) == 1
    assert reader.calls[0][0] == CHANNEL
    assert len(reader.calls[0][1]) == 15


async def test_old_posts_are_not_asked_about(db):
    """Пост недельной давности просмотры больше не набирает — не платим за него
    запросом."""
    publish(db, 1, published_at=NOW - timedelta(days=7))
    publish(db, 2, published_at=NOW - timedelta(hours=2))
    reader = FakeReader({1: 5, 2: 7})

    await ViewsCollector(db, reader, MetricsSettings(views_window_hours=72)).tick(now=NOW)

    assert reader.calls[0][1] == (2,)


async def test_deleted_post_is_skipped(db):
    publish(db, 3)
    db.conn.execute("UPDATE published SET deleted_at='2026-09-05T10:00:00Z'")
    reader = FakeReader({3: 100})

    assert await ViewsCollector(db, reader, MetricsSettings()).tick(now=NOW) == 0
    assert not reader.calls


async def test_views_never_go_down(db):
    """Просмотры только растут. Меньшее число — сбой замера, а не падение
    охвата: перезаписав его, мы бы испортили историю канала."""
    publish(db, 4, views=900)
    await ViewsCollector(db, FakeReader({4: 12}), MetricsSettings()).tick(now=NOW)

    assert db.conn.execute("SELECT views FROM published").fetchone()["views"] == 900


async def test_unreachable_channel_does_not_break_the_tick(db):
    """Юзербота не пустили в канал — конвейер продолжает работать, просмотры
    остаются пустыми, а причина уходит в алерт (не в тишину)."""
    publish(db, 5)
    alerts = []

    collector = ViewsCollector(db, FakeReader(error=ValueError("нет доступа к каналу")),
                               MetricsSettings(), alerter=_Alerter(alerts))
    assert await collector.tick(now=NOW) == 0
    assert db.conn.execute("SELECT views FROM published").fetchone()["views"] is None
    assert alerts and "просмотр" in alerts[0].lower()


async def test_coverage_shows_whether_measurement_works(db):
    publish(db, 6, views=10)
    publish(db, 7)
    assert store.views_coverage(db, NOW, window_hours=24) == (2, 1)


class _Alerter:
    def __init__(self, sink):
        self.sink = sink

    async def alert(self, message):
        self.sink.append(message)
        return True


async def test_a_post_deleted_by_hand_is_marked_deleted(db):
    """Пост удалили в клиенте, минуя админ-бота: сообщения в канале больше нет.
    Такой пост надо отметить удалённым — иначе он навсегда останется в охвате
    и однажды попадёт ссылкой в вечерний топ-5.
    """
    publish(db, 8)
    publish(db, 9, views=100)

    await ViewsCollector(db, FakeReader({9: 100}, missing=[8]), MetricsSettings()).tick(now=NOW)

    rows = {r["message_id"]: r["deleted_at"] for r in
            db.conn.execute("SELECT message_id, deleted_at FROM published")}
    assert rows["8"] is not None, "исчезнувший пост отмечен удалённым"
    assert rows["9"] is None


async def test_deleted_post_is_not_asked_about_again(db):
    publish(db, 8)
    reader = FakeReader(missing=[8])
    collector = ViewsCollector(db, reader, MetricsSettings())

    await collector.tick(now=NOW)
    await collector.tick(now=NOW)

    assert len(reader.calls) == 1, "про удалённый пост второй раз не спрашиваем"
