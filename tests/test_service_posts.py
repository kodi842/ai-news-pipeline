"""Сервисные автопосты (спека §8): утренняя погода и вечерний топ-5 дня.

Это наш собственный контент: фактуры источников в нём нет, рерайта нет,
LLM не участвует. Поэтому такие посты идут в очередь сразу одобренными —
в отличие от новостей, где кнопку жмёт модератор.
"""
from datetime import datetime, timezone

import pytest

from tgtraff import drafts as drafts_store
from tgtraff.db import Database
from tgtraff.publisher import store
from tgtraff.publisher.channels import ChannelsConfig, CityChannels, TelegramChannel, VkGroup
from tgtraff.publisher.service import ServicePosts
from tgtraff.publisher.settings import PublisherSettings, parse_slots
from tgtraff.publisher.weather import Forecast, describe_weather

CITY, TZ = "cherepovets", "Europe/Moscow"      # UTC+3


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def channels():
    city = CityChannels(city=CITY, deep_link="che",
                        tg=TelegramChannel(chat_id="-1001", username="example_channel_1",
                                           title="ЧП Череповец"),
                        vk=VkGroup(), slots=parse_slots(["18:00-22:00"]),
                        min_gap_minutes=20, max_gap_minutes=40, daily_limit=18)
    return ChannelsConfig(mode="test", ugc_bot="example_channel_3", cities={CITY: city})


FORECAST = Forecast(t_min=9.0, t_max=17.0, code=3, wind=5.4, precipitation=0.0)


class FakeWeather:
    def __init__(self, forecast=FORECAST, fail=False):
        self.forecast, self.fail = forecast, fail
        self.calls = 0

    async def forecast_for(self, lat, lon, tz):
        self.calls += 1
        if self.fail:
            raise RuntimeError("open-meteo не отвечает")
        return self.forecast


def service(db, weather=None, **kw):
    cities = {CITY: {"name": "Череповец", "tz": TZ, "lat": 59.13, "lon": 37.91}}
    return ServicePosts(db, channels(), cities, PublisherSettings(**kw),
                        weather=weather or FakeWeather())


def utc(hh, mm=0, day=4):
    return datetime(2026, 9, day, hh, mm, tzinfo=timezone.utc)


# --- утренняя погода ---------------------------------------------------------

async def test_weather_post_is_created_at_the_local_hour(db):
    ids = await service(db).ensure(now=utc(5, 5))          # 08:05 локального
    row = drafts_store.get_draft(db, ids[0])
    assert row["origin"] == "service" and row["category"] == "weather"
    assert "Череповц" in row["title"]
    assert "+17" in row["body"] and "+9" in row["body"]


async def test_weather_post_is_queued_without_moderation(db):
    ids = await service(db).ensure(now=utc(5, 5))
    assert drafts_store.get_draft(db, ids[0])["status"] == "approved"
    assert store.queue_row_for_draft(db, ids[0])["status"] == "queued"


async def test_weather_post_is_created_once_a_day(db):
    svc = service(db)
    await svc.ensure(now=utc(5, 5))
    assert await svc.ensure(now=utc(6, 0)) == []


async def test_nothing_happens_before_the_hour(db):
    weather = FakeWeather()
    assert await service(db, weather).ensure(now=utc(3, 0)) == []     # 06:00 локального
    assert weather.calls == 0


async def test_long_past_hour_is_not_caught_up(db):
    """Процесс лежал полдня — утренняя погода в обед уже не новость."""
    assert await service(db).ensure(now=utc(11, 0)) == []             # 14:00 локального


async def test_weather_api_failure_leaves_no_post(db):
    assert await service(db, FakeWeather(fail=True)).ensure(now=utc(5, 5)) == []


def test_weather_code_is_translated_to_words():
    text, emoji = describe_weather(3)
    assert text and emoji
    assert describe_weather(95)[0] != text


# --- вечерний топ-5 ----------------------------------------------------------

def published(db, title, views=None, sources=1, at="2026-09-04T10:00:00Z"):
    did = drafts_store.save_draft(db, cluster_id=None, city=CITY, title=title, body="Тело",
                                  category="dtp", status="published",
                                  sources=[f"s{i}" for i in range(sources)])
    pid = store.record_publication(db, draft_id=did, city=CITY, platform="tg",
                                   channel="-1001", message_id=str(1000 + did),
                                   url=f"https://t.me/example_channel_1/{1000 + did}")
    db.conn.execute("UPDATE published SET published_at=?, views=? WHERE id=?", (at, views, pid))
    return did


async def test_top5_collects_the_days_posts_with_links(db):
    published(db, "Первая новость", views=900)
    published(db, "Вторая новость", views=1500)
    published(db, "Третья новость", views=10)
    ids = await service(db).ensure(now=utc(18, 5))         # 21:05 локального
    body = drafts_store.get_draft(db, ids[0])["body"]
    assert body.index("Вторая новость") < body.index("Первая новость")   # по просмотрам
    assert "t.me/example_channel_1/" in body


async def test_top5_falls_back_to_the_number_of_sources(db):
    """Просмотров ещё нет (метрики — фаза 6): порядок держит число источников."""
    published(db, "Одинокая заметка", sources=1)
    published(db, "Новость всего города", sources=4)
    published(db, "Ещё одна заметка", sources=2)
    ids = await service(db).ensure(now=utc(18, 5))
    body = drafts_store.get_draft(db, ids[0])["body"]
    assert body.index("Новость всего города") < body.index("Одинокая заметка")


async def test_top5_keeps_at_most_five(db):
    for i in range(8):
        published(db, f"Новость {i}", views=i * 10)
    ids = await service(db).ensure(now=utc(18, 5))
    assert drafts_store.get_draft(db, ids[0])["body"].count("t.me/") == 5


async def test_no_top5_without_published_posts(db):
    assert await service(db).ensure(now=utc(18, 5)) == []


async def test_top5_ignores_yesterdays_posts(db):
    published(db, "Вчерашняя", at="2026-09-03T10:00:00Z")
    assert await service(db).ensure(now=utc(18, 5)) == []


async def test_service_posts_can_be_switched_off(db):
    assert await service(db, service_enabled=False).ensure(now=utc(5, 5)) == []


# --- сколько новостей достойны «топа» ----------------------------------------

async def test_top_needs_at_least_three_posts(db):
    """Живой дефект: «Топ-5 дня» из одной строки — не топ. В день с двумя
    публикациями вечернего поста быть не должно."""
    published(db, "Первая", views=100)
    published(db, "Вторая", views=90)
    assert await service(db).ensure(now=utc(18, 5)) == []


async def test_short_top_is_titled_as_the_main_news_of_the_day(db):
    for i in range(3):
        published(db, f"Новость {i}", views=i * 10)
    ids = await service(db).ensure(now=utc(18, 5))
    assert drafts_store.get_draft(db, ids[0])["title"] == "Главное за день в Череповце"


async def test_full_top_keeps_its_name(db):
    for i in range(5):
        published(db, f"Новость {i}", views=i * 10)
    ids = await service(db).ensure(now=utc(18, 5))
    assert drafts_store.get_draft(db, ids[0])["title"] == "Топ-5 дня в Череповце"


async def test_views_take_over_from_the_proxy_when_metrics_arrive(db):
    """Критерий фазы 6: как только просмотры сняты, топ переключается на них.

    Прокси фазы 5 (число источников) должен уступить: новость, о которой
    написал весь город, но которую никто не открыл, — не главная новость дня.
    """
    published(db, "Написали все, читать не стали", sources=5, views=120)
    published(db, "Один источник, но прочитали", sources=1, views=4200)
    published(db, "Третья новость дня", sources=2, views=300)
    ids = await service(db).ensure(now=utc(18, 5))
    body = drafts_store.get_draft(db, ids[0])["body"]
    assert body.index("Один источник, но прочитали") < body.index("Написали все")
