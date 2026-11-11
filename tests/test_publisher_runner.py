"""Publisher (спека §8): очередь → доставки → запись message_id.

Проверяется главное: раньше слота ничего не выходит, упавшая доставка
возвращается в очередь, а опубликованное можно найти по draft_id — иначе
админ-бот не сможет ни поправить, ни удалить пост.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tgtraff import drafts as drafts_store
from tgtraff.db import Database
from tgtraff.publisher import store
from tgtraff.publisher.channels import ChannelsConfig, CityChannels, TelegramChannel, VkGroup
from tgtraff.publisher.delivery import DeliveryError, DeliveryResult
from tgtraff.publisher.runner import Publisher
from tgtraff.publisher.settings import PublisherSettings, parse_slots

CITY = "cherepovets"
NOW = datetime(2026, 9, 4, 15, 30, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def channels(vk_on=False):
    city = CityChannels(
        city=CITY, deep_link="che",
        tg=TelegramChannel(chat_id="-1001", username="example_channel_1", title="ЧП Череповец"),
        vk=VkGroup(enabled=vk_on, group_id="42", token="t") if vk_on else VkGroup(),
        slots=parse_slots(["18:00-22:00"]), min_gap_minutes=20, max_gap_minutes=40,
        daily_limit=18)
    return ChannelsConfig(mode="test", ugc_bot="example_channel_3", cities={CITY: city})


class FakeDelivery:
    def __init__(self, platform="tg", fail=False):
        self.platform = platform
        self.fail = fail
        self.posts: list[tuple] = []

    async def publish(self, city, post, media_path):
        if self.fail:
            raise DeliveryError("канал недоступен")
        self.posts.append((city.city, post, media_path))
        return DeliveryResult(self.platform, city.tg.chat_id or "vk", "777",
                              f"https://example/{self.platform}/777")


class Alerts:
    def __init__(self):
        self.sent: list[str] = []

    async def alert(self, message):
        self.sent.append(message)
        return True


def publisher(db, deliveries=None, alerter=None, vk_on=False, **settings):
    return Publisher(db, channels(vk_on), deliveries or {"tg": FakeDelivery()},
                     PublisherSettings(**settings), alerter=alerter)


def queued(db, when=NOW, **kw):
    did = drafts_store.save_draft(db, cluster_id=None, city=CITY,
                                  title=kw.pop("title", "Хук"), body="Тело поста.",
                                  category="dtp", urgency=kw.pop("urgency", "normal"),
                                  status="approved", **kw)
    return did, store.enqueue(db, did, CITY, when, urgency=kw.get("urgency", "normal"))


async def test_due_post_is_published_and_mapped_to_a_message_id(db):
    did, qid = queued(db)
    outcomes = await publisher(db).tick(now=NOW)
    assert [o.status for o in outcomes] == ["published"]
    assert store.get_queued(db, qid)["status"] == "published"
    rows = store.publications(db, did)
    assert rows[0]["message_id"] == "777" and rows[0]["platform"] == "tg"


async def test_publication_marks_the_draft_published(db):
    did, _ = queued(db)
    await publisher(db).tick(now=NOW)
    assert drafts_store.get_draft(db, did)["status"] == "published"


async def test_nothing_is_published_before_its_slot(db):
    queued(db, when=NOW + timedelta(hours=2))
    tg = FakeDelivery()
    assert await publisher(db, {"tg": tg}).tick(now=NOW) == []
    assert tg.posts == []


async def test_rendered_post_carries_hook_hashtag_and_signature(db):
    queued(db, district="Индустриальный район")
    tg = FakeDelivery()
    await publisher(db, {"tg": tg}).tick(now=NOW)
    text = tg.posts[0][1].text
    assert "<b>Хук</b>" in text and "#Индустриальный" in text
    assert "example_channel_1" in text


async def test_media_of_the_draft_is_attached(db):
    queued(db, media_path="/m/a.jpg")
    tg = FakeDelivery()
    await publisher(db, {"tg": tg}).tick(now=NOW)
    assert tg.posts[0][2] == "/m/a.jpg"


async def test_failed_delivery_returns_the_post_to_the_queue(db):
    _, qid = queued(db)
    await publisher(db, {"tg": FakeDelivery(fail=True)}).tick(now=NOW)
    row = store.get_queued(db, qid)
    assert row["status"] == "queued" and row["attempts"] == 1
    assert "недоступен" in row["last_error"]


async def test_post_that_never_goes_through_fails_and_alerts(db):
    _, qid = queued(db)
    alerts = Alerts()
    pub = publisher(db, {"tg": FakeDelivery(fail=True)}, alerter=alerts, max_attempts=2)
    await pub.tick(now=NOW)
    await pub.tick(now=NOW)
    assert store.get_queued(db, qid)["status"] == "failed"
    assert any("публикац" in m.lower() for m in alerts.sent)


async def test_post_goes_to_every_configured_platform(db):
    did, _ = queued(db)
    tg, vk = FakeDelivery(), FakeDelivery("vk")
    await publisher(db, {"tg": tg, "vk": vk}, vk_on=True).tick(now=NOW)
    assert len(tg.posts) == 1 and len(vk.posts) == 1
    assert {r["platform"] for r in store.publications(db, did)} == {"tg", "vk"}


async def test_vk_failure_does_not_block_telegram(db):
    did, qid = queued(db)
    tg = FakeDelivery()
    await publisher(db, {"tg": tg, "vk": FakeDelivery("vk", fail=True)},
                    vk_on=True).tick(now=NOW)
    assert store.get_queued(db, qid)["status"] == "published"
    assert [r["platform"] for r in store.publications(db, did)] == ["tg"]


async def test_vk_gets_plain_text_without_html(db):
    queued(db, district="Индустриальный район")
    vk = FakeDelivery("vk")
    await publisher(db, {"tg": FakeDelivery(), "vk": vk}, vk_on=True).tick(now=NOW)
    assert "<b>" not in vk.posts[0][1]


async def test_city_without_a_channel_is_not_published_but_reported(db):
    did = drafts_store.save_draft(db, cluster_id=None, city="magnitogorsk", title="Хук",
                                  body="Тело", category="dtp", status="approved")
    store.enqueue(db, did, "magnitogorsk", NOW)
    alerts = Alerts()
    outcomes = await publisher(db, alerter=alerts).tick(now=NOW)
    assert [o.status for o in outcomes] == ["skipped"]
    assert alerts.sent


async def test_second_tick_does_not_publish_the_same_post_twice(db):
    queued(db)
    tg = FakeDelivery()
    pub = publisher(db, {"tg": tg})
    await pub.tick(now=NOW)
    await pub.tick(now=NOW)
    assert len(tg.posts) == 1


async def test_stuck_item_returns_to_the_queue(db):
    """Процесс убили на полпути: запись осталась в `sending` и должна вернуться."""
    _, qid = queued(db)
    store.claim(db, qid)
    db.conn.execute("UPDATE publish_queue SET updated_at='2026-09-01T00:00:00Z' WHERE id=?",
                    (qid,))
    await publisher(db).tick(now=NOW)
    assert store.get_queued(db, qid)["status"] == "published"


async def test_published_callback_lets_the_admin_bot_update_the_card(db):
    did, _ = queued(db)
    seen = []
    pub = publisher(db)
    pub.on_published = lambda outcome: seen.append(outcome.draft_id)
    await pub.tick(now=NOW)
    assert seen == [did]


def test_service_draft_is_rendered_as_trusted_html(db):
    """Тело сервисного поста собрано нами (ссылки топ-5) — его не экранируем."""
    from tgtraff.publisher.runner import content_of

    did = drafts_store.save_draft(db, cluster_id=None, city=CITY, title="Топ-5 дня",
                                  body='1. <a href="u">Новость</a>', category="city",
                                  origin="service", flags=["service"], status="approved")
    assert content_of(drafts_store.get_draft(db, did)).body_html is True


def test_news_draft_is_not_trusted(db):
    from tgtraff.publisher.runner import content_of

    did = drafts_store.save_draft(db, cluster_id=None, city=CITY, title="Хук",
                                  body="Тело", category="dtp", status="approved")
    assert content_of(drafts_store.get_draft(db, did)).body_html is False
