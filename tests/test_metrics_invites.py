"""Инвайт-ссылки под источники трафика и CPF-отчёт (спека §9).

«Каждому посеву — своя инвайт-ссылка»: ссылки заводятся ЗАРАНЕЕ, до первого
рубля закупа (пункт 5 чек-листа прогрева). Считает вступления Bot API: бот —
админ канала и видит `chat_member` с той ссылкой, по которой человек пришёл.
"""
from datetime import datetime, timezone

import pytest

from tgtraff.db import Database
from tgtraff.metrics import store
from tgtraff.metrics.invites import InviteLinks, ORGANIC
from tgtraff.metrics.report import cpf_report
from tgtraff.metrics.settings import MetricsSettings
from tgtraff.publisher.channels import ChannelsConfig, CityChannels, TelegramChannel, VkGroup
from tgtraff.publisher.settings import parse_slots

CITY, CHAT = "cherepovets", "-1001"
NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)

SOURCES = (
    {"name": "vk_target", "title": "VK-таргет по гео", "kind": "paid"},
    {"name": "vk_seeding", "title": "Посевы в пабликах", "kind": "paid"},
    {"name": "clips", "title": "VK Клипы", "kind": "organic"},
)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def channels(chat_id=CHAT):
    city = CityChannels(city=CITY, deep_link="che",
                        tg=TelegramChannel(chat_id=chat_id, username="example_channel_1"),
                        vk=VkGroup(), slots=parse_slots(["18:00-22:00"]),
                        min_gap_minutes=20, max_gap_minutes=40, daily_limit=18)
    return ChannelsConfig(mode="test", ugc_bot="example_channel_3", cities={CITY: city})


class FakeLink:
    def __init__(self, url, name):
        self.invite_link, self.name = url, name


class FakeBot:
    def __init__(self, error=None):
        self.created = []
        self.error = error

    async def create_chat_invite_link(self, chat_id, name=None, **kw):
        if self.error:
            raise self.error
        self.created.append((str(chat_id), name))
        return FakeLink(f"https://t.me/+{name}_{len(self.created)}", name)


def links(db, bot=None, settings=None, **kw):
    return InviteLinks(db, bot or FakeBot(), channels(**kw),
                       settings or MetricsSettings(invite_sources=SOURCES),
                       city_tz={CITY: "Europe/Moscow"})


# ---- заведение ссылок заранее ----

async def test_a_link_is_created_for_every_planned_traffic_source(db):
    bot = FakeBot()
    created = await links(db, bot).ensure(now=NOW)

    assert len(created) == 3
    # В Telegram у ссылки видно человеческое имя — оператор смотрит на список
    # ссылок канала глазами, а не на слаги конфига.
    assert [name for _, name in bot.created] == ["VK-таргет по гео", "Посевы в пабликах",
                                                 "VK Клипы"]
    rows = store.links(db, CITY)
    assert {r["name"] for r in rows} == {"vk_target", "vk_seeding", "clips"}
    assert all(r["url"] for r in rows)


async def test_links_are_not_created_twice(db):
    bot = FakeBot()
    await links(db, bot).ensure(now=NOW)
    await links(db, bot).ensure(now=NOW)

    assert len(bot.created) == 3, "повторный вызов не должен плодить двойников"
    assert len(store.links(db, CITY)) == 3


async def test_link_creation_failure_is_reported_not_swallowed(db):
    alerts = []
    lk = InviteLinks(db, FakeBot(error=RuntimeError("нет права приглашать")), channels(),
                     MetricsSettings(invite_sources=SOURCES), city_tz={CITY: "Europe/Moscow"},
                     alerter=_Alerter(alerts))

    assert await lk.ensure(now=NOW) == []
    assert alerts and "ссылк" in alerts[0].lower()


async def test_city_without_a_channel_is_skipped(db):
    bot = FakeBot()
    await links(db, bot, chat_id=None).ensure(now=NOW)
    assert not bot.created


# ---- счёт вступлений ----

async def test_join_through_a_link_is_counted_for_that_link(db):
    lk = links(db)
    created = await lk.ensure(now=NOW)
    url = created[0]["url"]

    lk.joined(CHAT, url, now=NOW)
    lk.joined(CHAT, url, now=NOW)

    row = store.link_by_url(db, url)
    assert row["joins_total"] == 2
    assert store.joins_in_days(db, row["id"], ("2026-09-05",)) == 2


async def test_join_without_a_link_goes_to_organic(db):
    """Пришли из поиска или по репосту — это тоже прирост, но не купленный.
    Смешивать его с посевом нельзя: CPF посева станет неправдой."""
    lk = links(db)
    await lk.ensure(now=NOW)

    lk.joined(CHAT, None, now=NOW)

    row = store.get_link(db, CITY, CHAT, ORGANIC)
    assert row["joins_total"] == 1
    assert row["kind"] == "organic"


async def test_join_by_day_separates_yesterday_from_today(db):
    lk = links(db)
    url = (await lk.ensure(now=NOW))[0]["url"]
    lk.joined(CHAT, url, now=datetime(2026, 9, 4, 12, tzinfo=timezone.utc))
    lk.joined(CHAT, url, now=NOW)

    link_id = store.link_by_url(db, url)["id"]
    assert store.joins_in_days(db, link_id, ("2026-09-04",)) == 1
    assert store.joins_in_days(db, link_id, ("2026-09-05",)) == 1


async def test_unknown_link_is_registered_on_the_fly(db):
    """Ссылку могли создать руками в клиенте Telegram — вступление по ней
    теряться не должно."""
    lk = links(db)
    lk.joined(CHAT, "https://t.me/+ruchnaya", now=NOW)

    row = store.link_by_url(db, "https://t.me/+ruchnaya")
    assert row is not None and row["joins_total"] == 1


# ---- CPF ----

def test_cpf_is_money_divided_by_joins(db):
    link = store.ensure_link(db, CITY, CHAT, "vk_seeding", url="u1", kind="paid")
    store.set_cost(db, link["id"], 9000)
    store.record_join(db, link["id"], "2026-09-05", count=300)

    report = cpf_report(db, now=NOW)
    row = next(r for r in report.rows if r.name == "vk_seeding")
    assert row.joins == 300
    assert row.cpf == pytest.approx(30.0)


def test_cpf_is_empty_for_organic_traffic(db):
    """У органики нет знаменателя в рублях — CPF у неё не бывает, и ноль
    вместо него был бы враньём в отчёте гейта."""
    link = store.ensure_link(db, CITY, CHAT, "clips", url="u2", kind="organic")
    store.record_join(db, link["id"], "2026-09-05", count=50)

    row = next(r for r in cpf_report(db, now=NOW).rows if r.name == "clips")
    assert row.cpf is None
    assert row.joins == 50


def test_cpf_of_a_placement_without_joins_is_not_a_division_by_zero(db):
    link = store.ensure_link(db, CITY, CHAT, "vk_target", url="u3", kind="paid")
    store.set_cost(db, link["id"], 15000)

    row = next(r for r in cpf_report(db, now=NOW).rows if r.name == "vk_target")
    assert row.cpf is None
    assert row.cost == 15000


def test_report_renders_a_table_with_totals(db):
    paid = store.ensure_link(db, CITY, CHAT, "vk_seeding", url="u1", kind="paid")
    store.set_cost(db, paid["id"], 9000)
    store.record_join(db, paid["id"], "2026-09-05", count=300)
    free = store.ensure_link(db, CITY, CHAT, "clips", url="u2", kind="organic")
    store.record_join(db, free["id"], "2026-09-05", count=50)

    text = cpf_report(db, now=NOW).render()
    assert "vk_seeding" in text and "30" in text
    assert "350" in text, "в итоге должен быть весь прирост, а не только платный"


class _Alerter:
    def __init__(self, sink):
        self.sink = sink

    async def alert(self, message):
        self.sink.append(message)
        return True
