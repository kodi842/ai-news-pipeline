"""Планировщик очереди: слоты + паузы + дневной потолок вместе (спека §8).

Проверяется то, чего не видно в тестах отдельных функций: как ведёт себя
очередь, когда модератор одобряет подряд больше постов, чем город публикует
за сутки.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from tgtraff import drafts as drafts_store
from tgtraff.db import Database
from tgtraff.publisher import store
from tgtraff.publisher.channels import ChannelsConfig, CityChannels, TelegramChannel, VkGroup
from tgtraff.publisher.planner import Planner
from tgtraff.publisher.schedule import local_day
from tgtraff.publisher.settings import parse_slots

CITY, TZ = "cherepovets", "Europe/Moscow"
NOW = datetime(2026, 9, 4, 15, 30, tzinfo=timezone.utc)      # 18:30 локального


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def planner(daily_limit=18, **kw):
    city = CityChannels(city=CITY, deep_link="che",
                        tg=TelegramChannel(chat_id="-1001"), vk=VkGroup(),
                        slots=parse_slots(kw.pop("slots", ["07:00-09:00", "18:00-22:00"])),
                        min_gap_minutes=20, max_gap_minutes=40, daily_limit=daily_limit)
    ch = ChannelsConfig(mode="test", ugc_bot="example_channel_3", cities={CITY: city})
    return Planner(ch, {CITY: TZ})


def approve(db, p, n=1, now=NOW):
    """Ставит n постов в очередь так, как это делает кнопка модератора."""
    out = []
    for i in range(n):
        did = drafts_store.save_draft(db, cluster_id=None, city=CITY, title=f"#{i}",
                                      body="т", status="approved")
        when = p.plan(db, CITY, now=now)
        store.enqueue(db, did, CITY, when)
        out.append(when)
    return out


def test_daily_limit_is_kept_on_every_day_not_only_today(db):
    """Одобрили десять постов при потолке 2 — ни одни сутки не должны получить
    больше двух."""
    times = approve(db, planner(daily_limit=2), n=10)
    per_day = Counter(local_day(t, TZ) for t in times)
    assert max(per_day.values()) == 2
    assert len(per_day) == 5


def test_pause_is_kept_inside_a_day(db):
    times = approve(db, planner(daily_limit=4), n=4)
    same_day = [t for t in times if local_day(t, TZ) == local_day(times[0], TZ)]
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(same_day, same_day[1:])]
    assert all(20 <= g <= 40 for g in gaps), gaps


def test_flash_ignores_the_daily_limit(db):
    p = planner(daily_limit=1)
    approve(db, p, n=1)
    assert p.plan(db, CITY, urgency="flash", now=NOW) == NOW


def test_service_post_pushes_the_next_news_post_by_a_full_pause(db):
    """Проверка, оставленная ревью живого прогона фазы 5 на R6.

    На прогоне сервис-пост вышел через 20 с после новостного: сервис идёт мимо
    сетки by design. Но ОЧЕРЕДНОЙ новостной пост обязан держать паузу — и не от
    предыдущей новости, а от последней занятой минуты города, чем бы она ни была
    занята. Иначе лента после каждого топ-5 выдавала бы два поста подряд.
    """
    p = planner()
    news = approve(db, p, n=1)[0]
    # Сервисный автопост встаёт в очередь текущим моментом, планировщика не спрашивая.
    did = drafts_store.save_draft(db, cluster_id=None, city=CITY, title="Топ-5",
                                  body="т", origin="service", status="approved")
    store.enqueue(db, did, CITY, news + timedelta(seconds=20))

    following = p.plan(db, CITY, now=NOW)

    gap = (following - (news + timedelta(seconds=20))).total_seconds() / 60
    assert 20 <= gap <= 40, f"пауза после сервисного поста {gap:.1f} мин"


def test_pause_holds_across_a_long_run_of_approvals(db):
    """Двенадцать одобрений подряд — ни одной пары ближе 20 минут.

    Верхняя граница 40 мин — про соседей ВНУТРИ окна: между окнами (22:00 →
    07:00 и 09:00 → 18:00) разрыв законно больше, его задают слоты, а не пауза.
    """
    times = approve(db, planner(daily_limit=12), n=12)
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:])]
    assert all(g >= 20 for g in gaps), gaps

    inside_window = [g for g in gaps if g <= 60]
    assert len(inside_window) >= 6, "почти все посты должны идти подряд в окне"
    assert all(g <= 40 for g in inside_window), inside_window
