"""Закрепы каналов ботом: правила + UGC-оффер с кнопкой (CHANNEL-SETUP §60.5).

Руками закреп ставится текстом — инлайн-кнопка бывает только у поста, который
отправил бот. Поэтому «выпустить/перевыпустить закрепы» — команда оператора
админ-боту, и после неё на UGC-оффере появляется живая кнопка «📸 Прислать
новость» с deep-link'ом города.

Порядок из решения §60.3: сначала правила, потом оффер — в стопке закрепов
верхним видно последний, и верхним должен быть оффер.
"""
import pytest

from tgtraff.db import Database
from tgtraff.publisher.channels import (ChannelsConfig, CityChannels, TelegramChannel,
                                        VkGroup)
from tgtraff.publisher.pins import Pins, offer_text, rules_text
from tgtraff.publisher.settings import parse_slots

CITY = "cherepovets"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


class FakeMessage:
    def __init__(self, message_id):
        self.message_id = message_id


class FakeBot:
    def __init__(self, fail_on=None):
        self.calls: list[tuple[str, dict]] = []
        self.fail_on = fail_on
        self._next = 700

    async def _call(self, name, kw):
        self.calls.append((name, kw))
        if self.fail_on == name:
            raise RuntimeError("Bad Request: not enough rights")
        self._next += 1
        return FakeMessage(self._next)

    async def send_message(self, **kw):
        return await self._call("send_message", kw)

    async def pin_chat_message(self, **kw):
        return await self._call("pin_chat_message", kw)

    async def unpin_chat_message(self, **kw):
        return await self._call("unpin_chat_message", kw)

    async def delete_message(self, **kw):
        return await self._call("delete_message", kw)

    def names(self):
        return [n for n, _ in self.calls]

    def sent(self):
        return [kw for n, kw in self.calls if n == "send_message"]


def channels(chat_id="-1001", cities=(CITY,)):
    block = {}
    for slug in cities:
        block[slug] = CityChannels(
            city=slug, deep_link="che",
            tg=TelegramChannel(chat_id=chat_id, username="example_channel_1",
                               title="ЧП Череповец"),
            vk=VkGroup(), slots=parse_slots(["18:00-22:00"]),
            min_gap_minutes=20, max_gap_minutes=40, daily_limit=18)
    return ChannelsConfig(mode="test", ugc_bot="example_channel_3", cities=block)


# --- тексты ------------------------------------------------------------------

def test_rules_carry_no_sales_contact_until_sales_start():
    """Решение §60.4: строка «реклама — @…» опущена до заведения sales-аккаунта."""
    text = rules_text(channels().city(CITY), "example_channel_3")

    assert "реклам" in text.lower()           # запрет рекламы в правилах остаётся
    assert "@<sales>" not in text and "@sales" not in text


def test_offer_states_the_price_and_the_anonymity():
    text = offer_text(channels().city(CITY), "example_channel_3")

    assert "300" in text and "1000" in text
    assert "анонимн" in text.lower()


def test_offer_sends_the_reader_to_the_button_not_to_a_bare_link():
    """У поста бота есть кнопка (PINS.md §1A) — текст должен звать в неё."""
    assert "кнопк" in offer_text(channels().city(CITY), "example_channel_3").lower()


# --- выпуск ------------------------------------------------------------------

async def test_rules_go_first_and_the_offer_second(db):
    """§60.3: верхним в стопке виден последний закреп — им должен быть оффер."""
    bot = FakeBot()

    await Pins(db, bot, channels()).issue()

    first, second = bot.sent()[0]["text"], bot.sent()[1]["text"]
    assert "Правила" in first
    assert "ПРИСЛАТЬ НОВОСТЬ" in second


async def test_the_offer_carries_the_ugc_button_with_the_city_deep_link(db):
    bot = FakeBot()

    await Pins(db, bot, channels()).issue()

    markup = bot.sent()[1]["reply_markup"]
    button = markup.inline_keyboard[0][0]
    assert "Прислать новость" in button.text
    assert button.url == "https://t.me/example_channel_3?start=che"


async def test_rules_post_has_no_button(db):
    """Кнопка одна на канал и живёт на оффере — две одинаковые кнопки подряд
    в стопке закрепов только путают."""
    bot = FakeBot()

    await Pins(db, bot, channels()).issue()

    assert bot.sent()[0].get("reply_markup") is None


async def test_both_posts_are_pinned_without_waking_the_channel(db):
    bot = FakeBot()

    await Pins(db, bot, channels()).issue()

    pins = [kw for n, kw in bot.calls if n == "pin_chat_message"]
    assert len(pins) == 2
    assert all(kw["disable_notification"] for kw in pins)


async def test_issued_pins_are_remembered(db):
    await Pins(db, FakeBot(), channels()).issue()

    assert db.get_meta(f"pins:{CITY}")


async def test_reissue_removes_the_previous_pair_first(db):
    """«Перевыпустить» — заменить, а не наплодить: старая пара снимается
    с закрепа и удаляется, иначе в стопке копятся мёртвые офферы."""
    bot = FakeBot()
    pins = Pins(db, bot, channels())
    await pins.issue()
    old = [kw["message_id"] for n, kw in bot.calls if n == "pin_chat_message"]
    bot.calls.clear()

    await pins.issue()

    deleted = [kw["message_id"] for n, kw in bot.calls if n == "delete_message"]
    assert sorted(deleted) == sorted(old)
    assert bot.names().index("delete_message") < bot.names().index("send_message")


async def test_a_city_without_a_channel_is_skipped(db):
    bot = FakeBot()

    result = await Pins(db, bot, channels(chat_id=None)).issue()

    assert bot.sent() == []
    assert result.skipped == [CITY]


async def test_a_city_that_refuses_does_not_stop_the_others(db):
    """Бот не админ в одном канале — остальные каналы закрепы всё равно получают."""
    bot = FakeBot(fail_on="pin_chat_message")

    result = await Pins(db, bot, channels(cities=(CITY, "magnitogorsk"))).issue()

    assert result.failed and result.done == []


async def test_issue_reports_what_it_did_per_city(db):
    result = await Pins(db, FakeBot(), channels()).issue()

    assert result.done == [CITY]
    assert CITY in result.render()
