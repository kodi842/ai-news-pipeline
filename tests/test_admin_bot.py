"""Админ-бот модерации (спека §7 + §8): карточки, кнопки, правка ответом,
правка и удаление уже опубликованного.

Тесты работают с двойником aiogram-бота: проверяется поведение модерации,
а не библиотека.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tgtraff import drafts as drafts_store
from tgtraff.db import Database
from tgtraff.moderation import actions
from tgtraff.moderation.bot import AdminBot
from tgtraff.publisher import store
from tgtraff.publisher.channels import ChannelsConfig, CityChannels, TelegramChannel, VkGroup
from tgtraff.publisher.planner import Planner
from tgtraff.publisher.settings import ModerationSettings, parse_slots

CITY, TZ, MOD = "cherepovets", "Europe/Moscow", 777
NOW = datetime(2026, 9, 4, 15, 30, tzinfo=timezone.utc)      # 18:30 локального


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


class FakeMessage:
    def __init__(self, message_id):
        self.message_id = message_id


class FakeBot:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._next = 500

    async def _call(self, name, kw):
        self.calls.append((name, kw))
        self._next += 1
        return FakeMessage(self._next)

    async def send_message(self, **kw):
        return await self._call("send_message", kw)

    async def send_photo(self, **kw):
        return await self._call("send_photo", kw)

    async def edit_message_text(self, **kw):
        return await self._call("edit_message_text", kw)

    async def edit_message_reply_markup(self, **kw):
        return await self._call("edit_message_reply_markup", kw)

    async def edit_message_caption(self, **kw):
        return await self._call("edit_message_caption", kw)

    async def delete_message(self, **kw):
        return await self._call("delete_message", kw)

    async def edit_message_media(self, **kw):
        return await self._call("edit_message_media", kw)

    async def download(self, file_id, destination):
        """Bot API отдаёт файл по file_id; двойник просто кладёт байты."""
        self.calls.append(("download", {"file_id": file_id, "destination": destination}))
        from pathlib import Path as _P

        _P(destination).parent.mkdir(parents=True, exist_ok=True)
        _P(destination).write_bytes(b"jpeg-from-moderator")

    def texts(self):
        return [kw.get("text", "") or kw.get("caption", "") for _, kw in self.calls]

    def names(self):
        return [n for n, _ in self.calls]


def channels():
    city = CityChannels(city=CITY, deep_link="che",
                        tg=TelegramChannel(chat_id="-1001", username="example_channel_1",
                                           title="ЧП Череповец"),
                        vk=VkGroup(), slots=parse_slots(["18:00-22:00"]),
                        min_gap_minutes=20, max_gap_minutes=40, daily_limit=18)
    return ChannelsConfig(mode="test", ugc_bot="example_channel_3", cities={CITY: city})


def admin(db, bot=None, moderators=(MOD,), deliveries=None, autopublish=()):
    ch = channels()
    return AdminBot(db, bot or FakeBot(), ch, Planner(ch, {CITY: TZ}),
                    ModerationSettings(moderators=tuple(moderators)),
                    city_names={CITY: "Череповец"}, deliveries=deliveries or {},
                    autopublish_categories=autopublish)


def draft(db, **kw):
    # После §55 карточкой пушится только срочное: молния, UGC, manual_only.
    # Тестовый драфт по умолчанию manual_only — чтобы кнопки и правка
    # проверялись на пуше; обычный safe-драфт ждёт /queue (см. тесты §55).
    base = dict(cluster_id=None, city=CITY, title="Такси вылетело на тротуар",
                body="Пострадали два человека.", category="dtp", urgency="normal",
                safety_verdict="manual_only", sources=["example_channel_7"])
    return drafts_store.save_draft(db, **{**base, **kw})


# --- доступ ------------------------------------------------------------------

async def test_stranger_is_not_served(db):
    bot = FakeBot()
    a = admin(db, bot)
    assert a.is_moderator(MOD) and not a.is_moderator(123)
    await a.handle_start(chat_id=123, user_id=123, username="stranger")
    assert "нет в списке модераторов" in bot.texts()[0].lower()


async def test_start_tells_the_id_when_nobody_is_configured(db):
    """Первый запуск: список пуст — бот подсказывает, что положить в ADMIN_CHAT_IDS."""
    bot = FakeBot()
    await admin(db, bot, moderators=()).handle_start(chat_id=42, user_id=42, username="owner")
    assert "42" in bot.texts()[0]


# --- карточки ----------------------------------------------------------------

async def test_new_draft_is_pushed_as_a_card_with_buttons(db):
    did = draft(db)
    bot = FakeBot()
    assert await admin(db, bot).push_cards(now=NOW) == 1
    name, kw = bot.calls[0]
    assert name == "send_message"
    assert "Такси вылетело" in kw["text"]
    assert kw["reply_markup"].inline_keyboard
    assert drafts_store.get_draft(db, did)["card_message_id"] == 501


async def test_the_same_card_is_not_pushed_twice(db):
    draft(db)
    a = admin(db)
    await a.push_cards(now=NOW)
    assert await a.push_cards(now=NOW) == 0


async def test_draft_with_a_photo_is_pushed_with_the_photo(db, tmp_path):
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"jpeg")
    draft(db, media_kind="photo", media_path=str(photo))
    bot = FakeBot()
    await admin(db, bot).push_cards(now=NOW)
    assert bot.names()[0] == "send_photo"


async def test_stack_is_limited_per_tick(db):
    for _ in range(5):
        draft(db)
    a = admin(db)
    a.settings = ModerationSettings(moderators=(MOD,), cards_per_tick=2)
    assert await a.push_cards(now=NOW) == 2


# --- кнопки ------------------------------------------------------------------

async def test_publish_button_queues_the_draft(db):
    did = draft(db)
    bot = FakeBot()
    a = admin(db, bot)
    await a.push_cards(now=NOW)
    answer = await a.handle_callback(MOD, chat_id=MOD, message_id=501,
                                     data=f"pub:{did}", now=NOW)
    assert drafts_store.get_draft(db, did)["status"] == "approved"
    assert store.queue_row_for_draft(db, did)["status"] == "queued"
    assert "очеред" in answer.lower() or "18:30" in answer
    assert "edit_message_text" in bot.names()


async def test_stranger_cannot_press_buttons(db):
    did = draft(db)
    a = admin(db)
    await a.handle_callback(123, chat_id=123, message_id=1, data=f"pub:{did}", now=NOW)
    assert drafts_store.get_draft(db, did)["status"] == "new"


async def test_flash_button_says_it_goes_now(db):
    did = draft(db, urgency="flash")
    a = admin(db)
    await a.push_cards(now=NOW)
    answer = await a.handle_callback(MOD, MOD, 501, f"pub:{did}", now=NOW)
    assert "сейчас" in answer.lower() or "немедленно" in answer.lower()


async def test_now_button_publishes_a_normal_draft_out_of_turn(db):
    did = draft(db)
    a = admin(db)
    await a.push_cards(now=NOW)
    await a.handle_callback(MOD, MOD, 501, f"now:{did}", now=NOW)
    assert store.parse(store.queue_row_for_draft(db, did)["scheduled_at"]) == NOW


async def test_reject_button_asks_for_a_reason_first(db):
    did = draft(db)
    bot = FakeBot()
    a = admin(db, bot)
    await a.push_cards(now=NOW)
    await a.handle_callback(MOD, MOD, 501, f"rej:{did}", now=NOW)
    assert drafts_store.get_draft(db, did)["status"] == "new"      # решения ещё нет
    markup = [kw for n, kw in bot.calls if n == "edit_message_reply_markup"][-1]
    labels = [b.text for row in markup["reply_markup"].inline_keyboard for b in row]
    assert any("не новость" in x.lower() for x in labels)


async def test_reason_button_rejects_with_that_reason(db):
    did = draft(db)
    a = admin(db)
    await a.push_cards(now=NOW)
    await a.handle_callback(MOD, MOD, 501, f"why:{did}:low_facts", now=NOW)
    row = drafts_store.get_draft(db, did)
    assert row["status"] == "rejected" and row["reject_reason"] == "low_facts"


async def test_defer_button_postpones(db):
    did = draft(db)
    a = admin(db)
    await a.push_cards(now=NOW)
    await a.handle_callback(MOD, MOD, 501, f"defer:{did}", now=NOW)
    assert drafts_store.get_draft(db, did)["status"] == "deferred"


async def test_queue_command_returns_deferred_drafts(db):
    """Отложенный лежит в пуле §55 и возвращается карточкой, когда его тянут."""
    did = draft(db)
    a = admin(db)
    await a.push_cards(now=NOW)
    await a.handle_callback(MOD, MOD, 501, f"defer:{did}", now=NOW)
    await a.handle_queue(chat_id=MOD, user_id=MOD, args="1")
    assert drafts_store.get_draft(db, did)["status"] == "new"


# --- правка ответным сообщением ---------------------------------------------

async def test_edit_hint_mentions_the_photo_reply(db):
    """§63: подсказка после кнопки «Править» говорила только про текст, и
    оператор на живом тесте счёл фото-ответ (§61.1) отсутствующим."""
    did = draft(db)
    bot = FakeBot()
    a = admin(db, bot)
    await a.push_cards(now=NOW)
    await a.handle_callback(MOD, chat_id=MOD, message_id=501, data=f"edit:{did}")
    hint = bot.texts()[-1].lower()
    assert "фотограф" in hint
    assert "картинку" in hint or "картинка" in hint
    assert "подпись" in hint


async def test_reply_to_the_card_edits_the_draft(db):
    did = draft(db)
    a = admin(db)
    await a.push_cards(now=NOW)
    handled = await a.handle_reply(MOD, chat_id=MOD, reply_to_message_id=501,
                                   text="Новый хук\nНовое тело.")
    row = drafts_store.get_draft(db, did)
    assert handled and row["title"] == "Новый хук" and row["body"] == "Новое тело."


async def test_reply_to_a_foreign_message_is_ignored(db):
    draft(db)
    a = admin(db)
    await a.push_cards(now=NOW)
    assert await a.handle_reply(MOD, MOD, reply_to_message_id=999, text="что-то") is False


# --- правка и удаление опубликованного ---------------------------------------

class RecordingDelivery:
    platform = "tg"

    def __init__(self):
        self.edited, self.deleted = [], []

    async def edit(self, city, message_id, text, has_media):
        self.edited.append((message_id, text, has_media))

    async def delete(self, city, message_ids):
        self.deleted.append(list(message_ids))


async def test_edit_of_a_published_draft_rewrites_the_channel_post(db):
    tg = RecordingDelivery()
    did = draft(db, status="published")
    store.record_publication(db, draft_id=did, city=CITY, platform="tg", channel="-1001",
                             message_id="42")
    a = admin(db, deliveries={"tg": tg})
    a._cards[501] = did                                  # карточка уже была отправлена
    await a.handle_reply(MOD, MOD, reply_to_message_id=501, text="Исправленный хук")
    assert tg.edited and tg.edited[0][0] == "42"
    assert "Исправленный хук" in tg.edited[0][1]


async def test_delete_button_removes_every_message_of_the_post(db):
    tg = RecordingDelivery()
    did = draft(db, status="published")
    store.record_publication(db, draft_id=did, city=CITY, platform="tg", channel="-1001",
                             message_id="42", extra_message_ids=["41"])
    a = admin(db, deliveries={"tg": tg})
    await a.handle_callback(MOD, MOD, 501, f"del:{did}", now=NOW)
    assert tg.deleted == [["42", "41"]]
    assert store.publications(db, did) == []


async def test_publication_updates_the_card(db):
    did = draft(db)
    bot = FakeBot()
    a = admin(db, bot)
    await a.push_cards(now=NOW)
    outcome = SimpleNamespace(draft_id=did, city=CITY, status="published",
                              results=[SimpleNamespace(platform="tg", message_id="42",
                                                       url="https://t.me/x/42")])
    await a.announce_published(outcome)
    assert any("опубликован" in t.lower() for t in bot.texts())


async def test_now_button_keeps_the_post_normal(db):
    """Кнопка «Вне очереди» ускоряет публикацию, но не переписывает драфт в молнию."""
    did = draft(db)
    a = admin(db)
    await a.push_cards(now=NOW)
    await a.handle_callback(MOD, MOD, 501, f"now:{did}", now=NOW)
    assert drafts_store.get_draft(db, did)["urgency"] == "normal"


async def test_card_is_sent_to_every_moderator_and_any_reply_works(db):
    did = draft(db)
    bot = FakeBot()
    a = admin(db, bot, moderators=(MOD, 888))
    await a.push_cards(now=NOW)
    chats = [kw["chat_id"] for n, kw in bot.calls if n == "send_message"]
    assert chats == [MOD, 888]
    # ответ второго модератора на СВОЮ карточку тоже правит драфт
    second_card = bot.calls[1][1]
    assert second_card["chat_id"] == 888
    assert await a.handle_reply(888, 888, reply_to_message_id=502, text="Правка второго")
    assert drafts_store.get_draft(db, did)["title"] == "Правка второго"


async def test_long_card_with_a_photo_shows_both_photo_and_text(db, tmp_path):
    """Подпись к медиа — 1024 знака. Длинная карточка не должна лишать
    модератора картинки: сначала фото, следом текст с кнопками."""
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"jpeg")
    draft(db, body="Очень длинный текст. " * 120, media_kind="photo",
          media_path=str(photo))
    bot = FakeBot()
    await admin(db, bot).push_cards(now=NOW)
    assert bot.names() == ["send_photo", "send_message"]
    assert bot.calls[0][1].get("caption") is None
    assert bot.calls[1][1]["reply_markup"].inline_keyboard      # кнопки на тексте


class PickyBot(FakeBot):
    """Telegram отвечает ошибкой, если правишь подпись у текстового сообщения
    (и наоборот) — карточка обязана справиться и в этом случае."""

    async def edit_message_caption(self, **kw):
        self.calls.append(("edit_message_caption", kw))
        raise RuntimeError("Bad Request: there is no caption in the message to edit")


async def test_decided_card_loses_its_buttons_even_if_the_first_edit_fails(db, tmp_path):
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"jpeg")
    did = draft(db, media_kind="photo", media_path=str(photo))
    bot = PickyBot()
    a = admin(db, bot)
    await a.push_cards(now=NOW)
    await a.handle_callback(MOD, MOD, 501, f"defer:{did}", now=NOW)
    assert "edit_message_text" in bot.names()


async def test_bot_added_to_a_channel_reports_the_id(db):
    """Чтобы не искать id канала руками: бота добавили админом — он говорит,
    какой id и в какую переменную .env его положить."""
    bot = FakeBot()
    a = admin(db, bot)
    message = await a.handle_my_chat_member(
        chat_id=-1001234567890, title="ЧП Череповец ТЕСТ", chat_type="channel",
        status="administrator")
    assert "-1001234567890" in message and "TG_CHANNEL" in message
    assert any("-1001234567890" in t for t in bot.texts())


async def test_removal_from_a_channel_is_reported_too(db):
    bot = FakeBot()
    message = await admin(db, bot).handle_my_chat_member(
        chat_id=-100123, title="ЧП Череповец ТЕСТ", chat_type="channel", status="left")
    assert "больше не админ" in message.lower() or "исключ" in message.lower()


async def test_being_added_to_a_group_chat_is_not_reported_as_a_channel(db):
    assert await admin(db).handle_my_chat_member(
        chat_id=-400, title="Курилка", chat_type="group", status="member") is None


# --- §55: пуш только срочного, очередь по /queue, протухание -----------------

def aged(db, draft_id, hours):
    db.conn.execute("UPDATE drafts SET created_at=? WHERE id=?",
                    ((NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"), draft_id))


def card_ids(bot):
    """Какие драфты ушли карточками — по кнопке «Опубликовать» (сводка очереди
    тоже с кнопкой, но не карточка)."""
    out = []
    for n, kw in bot.calls:
        markup = kw.get("reply_markup")
        if n == "send_message" and markup:
            data = markup.inline_keyboard[0][0].callback_data
            if data.startswith("pub:"):
                out.append(int(data.split(":")[1]))
    return out


async def test_plain_draft_is_not_pushed_it_waits_in_the_queue(db):
    draft(db, safety_verdict="safe")
    bot = FakeBot()
    assert await admin(db, bot).push_cards(now=NOW) == 0
    assert bot.calls == []


async def test_flash_ugc_and_manual_only_are_pushed(db):
    draft(db, safety_verdict="safe", urgency="flash")
    draft(db, safety_verdict="safe", origin="ugc", flags=["ugc"])
    draft(db, safety_verdict="manual_only")
    assert await admin(db).push_cards(now=NOW) == 3


async def test_queue_with_a_number_pulls_that_many_fresh_cards(db):
    ids = [draft(db, safety_verdict="safe") for _ in range(3)]
    for i, did in enumerate(ids):
        aged(db, did, 3 - i)                         # последний — самый свежий
    bot = FakeBot()
    await admin(db, bot).handle_queue(chat_id=MOD, user_id=MOD, args="2")
    assert card_ids(bot) == [ids[2], ids[1]]


async def test_queue_without_a_number_shows_a_summary_not_cards(db):
    draft(db, safety_verdict="safe", category="dtp")
    draft(db, safety_verdict="safe", category="dtp")
    bot = FakeBot()
    await admin(db, bot).handle_queue(chat_id=MOD, user_id=MOD, args=None)
    assert card_ids(bot) == []
    text = bot.texts()[-1]
    assert "Череповец" in text and "ДТП" in text and "2" in text


async def test_summary_offers_the_default_batch_with_one_button(db):
    """«/queue без аргумента — сводка», «по умолчанию 10»: сводка заканчивается
    кнопкой, которая выдаёт пачку по умолчанию."""
    for _ in range(12):
        draft(db, safety_verdict="safe")
    bot = FakeBot()
    a = admin(db, bot)
    await a.handle_queue(chat_id=MOD, user_id=MOD, args=None)
    button = bot.calls[-1][1]["reply_markup"].inline_keyboard[0][0]
    assert "10" in button.text
    await a.handle_callback(MOD, MOD, 501, button.callback_data, now=NOW)
    assert len(card_ids(bot)) == 10


async def test_pulling_the_queue_revives_a_deferred_draft(db):
    did = draft(db)
    a = admin(db)
    await a.push_cards(now=NOW)
    await a.handle_callback(MOD, MOD, 501, f"defer:{did}", now=NOW)
    await a.handle_queue(chat_id=MOD, user_id=MOD, args="5")
    assert drafts_store.get_draft(db, did)["status"] == "new"


async def test_expired_card_is_rewritten_as_stale_and_loses_its_buttons(db):
    did = draft(db, urgency="flash")
    bot = FakeBot()
    a = admin(db, bot)
    await a.push_cards(now=NOW)
    aged(db, did, 7)
    assert await a.expire(now=NOW) == 1
    assert drafts_store.get_draft(db, did)["status"] == "stale"
    _, kw = [c for c in bot.calls if c[0] == "edit_message_text"][-1]
    assert kw["message_id"] == 501 and "протух" in kw["text"]
    assert "reply_markup" not in kw


async def test_button_on_a_stale_draft_is_refused(db):
    did = draft(db, urgency="flash")
    a = admin(db)
    await a.push_cards(now=NOW)
    aged(db, did, 7)
    await a.expire(now=NOW)
    answer = await a.handle_callback(MOD, MOD, 501, f"pub:{did}", now=NOW)
    assert "протух" in answer.lower()
    assert drafts_store.get_draft(db, did)["status"] == "stale"
    assert store.queue_row_for_draft(db, did) is None


async def test_expire_has_nothing_to_rewrite_for_a_draft_without_a_card(db):
    did = draft(db, safety_verdict="safe")
    aged(db, did, 25)
    bot = FakeBot()
    assert await admin(db, bot).expire(now=NOW) == 1
    assert drafts_store.get_draft(db, did)["status"] == "stale"
    assert bot.calls == []


class RetryLater(Exception):
    """Двойник TelegramRetryAfter: у настоящего тот же атрибут retry_after."""
    retry_after = 3


class BusyBot(FakeBot):
    """Первая правка упирается в лимит Telegram (429), вторая проходит."""

    def __init__(self):
        super().__init__()
        self.busy = 1

    async def edit_message_text(self, **kw):
        if self.busy:
            self.busy -= 1
            raise RetryLater()
        return await super().edit_message_text(**kw)


async def test_card_rewrite_waits_out_a_telegram_rate_limit(db):
    """Протухание после ночи — десятки правок подряд. На 429 ждём столько,
    сколько просит Telegram, и повторяем: протухшая карточка не должна
    остаться с живыми кнопками."""
    did = draft(db, urgency="flash")
    bot = BusyBot()
    a = admin(db, bot)
    naps = []

    async def nap(seconds):
        naps.append(seconds)

    a._sleep = nap
    await a.push_cards(now=NOW)
    aged(db, did, 7)
    await a.expire(now=NOW)
    assert naps and naps[0] >= RetryLater.retry_after
    assert bot.names()[-1] == "edit_message_text"


# --- старт при рвущемся Telegram --------------------------------------------

async def test_startup_waits_for_telegram_instead_of_dying(db):
    """Первый запрос при старте упёрся в таймаут — процесс не падает (и не
    уходит в рестарт-луп Docker'а), а повторяет с паузой, пока Telegram
    не ответит. Polling у aiogram и так с повторами — дыра была только здесь."""
    from aiogram.exceptions import TelegramNetworkError

    from tgtraff.moderation.bot import connect_bot

    class FlakyBot:
        def __init__(self):
            self.calls = 0

        async def me(self):
            self.calls += 1
            if self.calls < 3:
                raise TelegramNetworkError(method=None, message="Request timeout error")
            return SimpleNamespace(username="chp_admin_ops_bot")

    naps = []

    async def nap(seconds):
        naps.append(seconds)

    me = await connect_bot(FlakyBot(), sleep=nap)
    assert me.username == "chp_admin_ops_bot"
    assert len(naps) == 2 and all(s > 0 for s in naps)


async def test_startup_does_not_retry_a_bad_token(db):
    """Неверный токен — не сеть: ждать нечего, падаем сразу и внятно."""
    from aiogram.exceptions import TelegramUnauthorizedError

    from tgtraff.moderation.bot import connect_bot

    class DeadBot:
        async def me(self):
            raise TelegramUnauthorizedError(method=None, message="Unauthorized")

    async def nap(seconds):
        raise AssertionError("ждать не должны")

    with pytest.raises(TelegramUnauthorizedError):
        await connect_bot(DeadBot(), sleep=nap)


# --- §59: автопубликация нейтральной новости ---------------------------------

AUTO = ("weather", "utility", "city", "other")


async def test_autopost_goes_to_the_queue_without_a_card(db):
    did = draft(db, safety_verdict="safe", category="city")
    bot = FakeBot()
    assert await admin(db, bot, autopublish=AUTO).autopublish(now=NOW) == 1
    assert bot.calls == []                        # карточка-пуш на автопост не шлётся
    assert drafts_store.get_draft(db, did)["status"] == "approved"
    assert store.queue_row_for_draft(db, did)["status"] == "queued"


async def test_autopublish_is_off_when_the_list_is_empty(db):
    did = draft(db, safety_verdict="safe", category="city")
    assert await admin(db).autopublish(now=NOW) == 0
    assert drafts_store.get_draft(db, did)["status"] == "new"


async def test_autopublish_does_not_touch_what_needs_hands(db):
    draft(db, safety_verdict="manual_only", category="city")
    draft(db, safety_verdict="safe", category="crime")
    draft(db, safety_verdict="safe", category="city", urgency="flash")
    draft(db, safety_verdict="safe", category="city", flags=["unsupported_facts"])
    assert await admin(db, autopublish=AUTO).autopublish(now=NOW) == 0


async def test_autopublished_draft_never_becomes_a_card(db):
    """Ни следующим тиком пуша, ни по /queue: решение уже принято."""
    draft(db, safety_verdict="safe", category="city")
    bot = FakeBot()
    a = admin(db, bot, autopublish=AUTO)
    await a.autopublish(now=NOW)
    assert await a.push_cards(now=NOW) == 0
    await a.pull_cards(chat_id=MOD, limit=10)
    assert card_ids(bot) == []


async def test_announcement_says_the_post_went_out_without_a_moderator(db):
    """Разбор инцидента §59 начинается с вопроса «а это кто выпустил» — ответ
    должен быть виден прямо в сообщении о публикации, а не в базе."""
    did = draft(db, safety_verdict="safe", category="city")
    bot = FakeBot()
    a = admin(db, bot, autopublish=AUTO)
    await a.autopublish(now=NOW)
    outcome = SimpleNamespace(draft_id=did, city=CITY, status="published",
                              results=[SimpleNamespace(platform="tg", message_id="42",
                                                       url="https://t.me/x/42")])
    await a.announce_published(outcome)
    text = bot.texts()[-1]
    assert "опубликован" in text.lower() and "без модератора" in text.lower()


async def test_queue_summary_shows_autoposts_on_a_separate_line(db):
    draft(db, safety_verdict="safe", category="city")
    bot = FakeBot()
    a = admin(db, bot, autopublish=AUTO)
    await a.autopublish(now=NOW)
    await a.handle_queue(chat_id=MOD, user_id=MOD, args=None)
    assert "автопост" in bot.texts()[-1].lower()


# --- §61.2: активные ссылки на источники в карточке --------------------------

def cluster_with_post(db, url="https://t.me/example_channel_7/4242"):
    """Кластер из одной ТГ-публикации — минимум, на котором видно ссылку."""
    from tgtraff.config import SourceSpec

    sid = db.upsert_source(SourceSpec(city=CITY, source_type="tg", key="example_channel_7"))
    pid = db.insert_raw_post(source_id=sid, source_type="tg", external_id="4242",
                             published_at="2026-09-04T10:00:00+00:00",
                             text="Горит дом", url=url).id
    cur = db.conn.execute(
        "INSERT INTO clusters (city, status, lead_raw_post_id, size, first_seen_at, last_seen_at)"
        " VALUES (?, 'open', ?, 1, '2026-09-04T10:00:00+00:00', '2026-09-04T10:00:00+00:00')",
        (CITY, pid))
    db.conn.execute(
        "INSERT INTO cluster_members (cluster_id, raw_post_id, reason) VALUES (?,?,'new')",
        (cur.lastrowid, pid))
    return cur.lastrowid


async def test_pushed_card_links_the_source_to_its_original_post(db):
    draft(db, cluster_id=cluster_with_post(db))
    bot = FakeBot()
    await admin(db, bot).push_cards(now=NOW)
    assert '<a href="https://t.me/example_channel_7/4242">example_channel_7</a>' in bot.texts()[0]


async def test_decided_card_keeps_the_source_links(db):
    """Карточка после решения переписывается тем же текстом — ссылки на
    первоисточник остаются: по ним разбирают спорную публикацию потом."""
    did = draft(db, cluster_id=cluster_with_post(db))
    bot = FakeBot()
    a = admin(db, bot)
    await a.push_cards(now=NOW)
    await a.handle_callback(MOD, MOD, 501, f"defer:{did}", now=NOW)
    assert 'href="https://t.me/example_channel_7/4242"' in bot.texts()[-1]


# --- §61.1: фото от модератора ответом на карточку ---------------------------

class FakeDetector:
    def __init__(self, verdict="none", owner=None):
        self.value, self.owner = verdict, owner

    def verdict(self, media):
        return self.value, self.owner


def with_photos(a, tmp_path, verdict="none", owner=None):
    """Подключает боту приём фото (§61.1) с двойником детектора и плашки."""
    from tgtraff.moderation.photo import ModeratorPhoto

    def stamp(src, dst, city):
        from pathlib import Path as _P
        _P(dst).write_bytes(_P(src).read_bytes() + b"-stamped")

    a.photos = ModeratorPhoto(a.db, media_dir=tmp_path / "media",
                              detector=FakeDetector(verdict, owner), stamper=stamp)
    a.inbox_dir = tmp_path / "inbox"
    return a


async def test_photo_reply_replaces_the_draft_media(db, tmp_path):
    did = draft(db)
    bot = FakeBot()
    a = with_photos(admin(db, bot), tmp_path)
    await a.push_cards(now=NOW)

    assert await a.handle_photo_reply(MOD, MOD, 501, file_id="AgACfile") is True

    row = drafts_store.get_draft(db, did)
    assert row["media_kind"] == "photo" and row["media_reason"] == "moderator"
    assert Path(row["media_path"]).exists()


async def test_photo_caption_edits_the_text_the_same_way_a_reply_does(db, tmp_path):
    did = draft(db)
    a = with_photos(admin(db, FakeBot()), tmp_path)
    await a.push_cards(now=NOW)

    await a.handle_photo_reply(MOD, MOD, 501, file_id="f", caption="Новый хук\nНовое тело")

    row = drafts_store.get_draft(db, did)
    assert row["title"] == "Новый хук" and row["body"] == "Новое тело"


async def test_photo_from_a_stranger_is_ignored(db, tmp_path):
    did = draft(db)
    a = with_photos(admin(db, FakeBot()), tmp_path)
    await a.push_cards(now=NOW)

    assert await a.handle_photo_reply(123, 123, 501, file_id="f") is False
    assert drafts_store.get_draft(db, did)["media_kind"] != "photo"


async def test_photo_that_answers_no_card_is_ignored(db, tmp_path):
    a = with_photos(admin(db, FakeBot()), tmp_path)
    assert await a.handle_photo_reply(MOD, MOD, 999, file_id="f") is False


async def test_foreign_watermark_stops_and_asks_for_confirmation(db, tmp_path):
    """Спека §5: чужой знак не трогаем и молча не публикуем — спрашиваем."""
    did = draft(db)
    bot = FakeBot()
    a = with_photos(admin(db, bot), tmp_path, verdict="foreign", owner="Верстов.Инфо")
    await a.push_cards(now=NOW)

    await a.handle_photo_reply(MOD, MOD, 501, file_id="f")

    assert drafts_store.get_draft(db, did)["media_kind"] != "photo"   # не подменили
    text, markup = bot.calls[-1][1]["text"], bot.calls[-1][1].get("reply_markup")
    assert "Верстов.Инфо" in text
    assert markup and any("став" in b.text.lower() or "всё равно" in b.text.lower()
                          for row in markup.inline_keyboard for b in row)


async def test_confirmation_button_puts_the_foreign_photo_in(db, tmp_path):
    did = draft(db)
    bot = FakeBot()
    a = with_photos(admin(db, bot), tmp_path, verdict="foreign", owner="Верстов.Инфо")
    await a.push_cards(now=NOW)
    await a.handle_photo_reply(MOD, MOD, 501, file_id="f")

    await a.handle_callback(MOD, MOD, 501, f"photo:{did}", now=NOW)

    row = drafts_store.get_draft(db, did)
    assert row["media_kind"] == "photo"
    # Чужой кадр НАШИМ знаком не метится (спека §5).
    assert Path(row["media_path"]).read_bytes() == b"jpeg-from-moderator"


async def test_confirmation_without_a_pending_photo_says_so(db, tmp_path):
    """Кнопку нажали после рестарта процесса — ждущего кадра уже нет."""
    did = draft(db)
    a = with_photos(admin(db, FakeBot()), tmp_path)

    answer = await a.handle_callback(MOD, MOD, 501, f"photo:{did}", now=NOW)

    assert "пришлите" in answer.lower() or "нет" in answer.lower()


async def test_photo_to_a_published_post_swaps_the_picture_in_the_channel(db, tmp_path):
    from tgtraff.publisher.delivery import TelegramDelivery

    did = draft(db, media_kind="photo", media_path=str(tmp_path / "old.jpg"))
    (tmp_path / "old.jpg").write_bytes(b"old")
    bot = FakeBot()
    a = with_photos(admin(db, bot, deliveries={"tg": TelegramDelivery(bot)}), tmp_path)
    await a.push_cards(now=NOW)
    store.enqueue(db, did, CITY, NOW)
    store.record_publication(db, did, CITY, "tg", "-1001", "77")
    actions.mark_published(db, did)

    await a.handle_photo_reply(MOD, MOD, 501, file_id="f")

    assert "edit_message_media" in bot.names()


async def test_photo_to_a_published_text_post_says_it_cannot_be_swapped(db, tmp_path):
    """Ограничение платформы (§43): текстовое сообщение картинкой не станет.
    Молчаливого «готово» тут быть не должно — модератор должен узнать."""
    from tgtraff.publisher.delivery import TelegramDelivery

    did = draft(db, media_kind=None, media_path=None)
    bot = FakeBot()
    a = with_photos(admin(db, bot, deliveries={"tg": TelegramDelivery(bot)}), tmp_path)
    await a.push_cards(now=NOW)
    store.enqueue(db, did, CITY, NOW)
    store.record_publication(db, did, CITY, "tg", "-1001", "77")
    actions.mark_published(db, did)

    await a.handle_photo_reply(MOD, MOD, 501, file_id="f")

    said = bot.texts()[-1].lower()
    assert "edit_message_media" not in bot.names()
    assert "в канале ничего не изменилось" in said       # не выдаём это за успех
    assert "удалить" in said                             # и говорим, чем лечится


# --- закрепы ботом (CHANNEL-SETUP §60.5) -------------------------------------

class FakePinBot(FakeBot):
    async def pin_chat_message(self, **kw):
        return await self._call("pin_chat_message", kw)

    async def unpin_chat_message(self, **kw):
        return await self._call("unpin_chat_message", kw)


def with_pins(a, bot):
    from tgtraff.publisher.pins import Pins

    a.pins = Pins(a.db, bot, a.channels)
    return a


async def test_pins_command_is_not_served_to_strangers(db):
    bot = FakePinBot()
    a = with_pins(admin(db, bot), bot)
    await a.handle_pins(chat_id=123, user_id=123, args=None)
    assert bot.calls == []


async def test_pins_command_asks_before_posting_to_channels(db):
    """Закреп уходит в боевой канал всем подписчикам — команда сначала
    показывает, куда именно, и ждёт кнопки."""
    bot = FakePinBot()
    a = with_pins(admin(db, bot), bot)

    await a.handle_pins(chat_id=MOD, user_id=MOD, args=None)

    assert "pin_chat_message" not in bot.names()          # ничего не выпущено
    assert CITY in bot.texts()[-1]
    assert bot.calls[-1][1]["reply_markup"].inline_keyboard


async def test_pins_button_issues_them(db):
    bot = FakePinBot()
    a = with_pins(admin(db, bot), bot)
    await a.handle_pins(chat_id=MOD, user_id=MOD, args=None)

    await a.handle_callback(MOD, MOD, 501, "pins:all", now=NOW)

    pinned = [kw for n, kw in bot.calls if n == "pin_chat_message"]
    assert len(pinned) == 2                                # правила + оффер
    assert any("ПРИСЛАТЬ НОВОСТЬ" in t for t in bot.texts())


async def test_pins_plan_names_the_mode_it_will_post_into(db):
    """Закреп в ТЕСТОВЫЙ канал и закреп в БОЕВОЙ — разные события. Команда
    одна и та же, поэтому режим должен быть виден до нажатия кнопки."""
    bot = FakePinBot()
    a = with_pins(admin(db, bot), bot)

    await a.handle_pins(chat_id=MOD, user_id=MOD, args=None)

    assert "ТЕСТОВ" in bot.texts()[-1]


async def test_pins_can_be_issued_for_one_city(db):
    bot = FakePinBot()
    a = with_pins(admin(db, bot), bot)

    await a.handle_pins(chat_id=MOD, user_id=MOD, args="magnitogorsk")

    assert "не знаю такого города" in bot.texts()[-1].lower()


async def test_caption_survives_the_watermark_confirmation(db, tmp_path):
    """Модератор прислал чужое фото С ПОДПИСЬЮ и подтвердил кнопкой: правка
    текста не должна потеряться по дороге через подтверждение."""
    did = draft(db)
    a = with_photos(admin(db, FakeBot()), tmp_path, verdict="foreign", owner="Верстов")
    await a.push_cards(now=NOW)
    await a.handle_photo_reply(MOD, MOD, 501, file_id="f", caption="Новый хук\nНовое тело")

    await a.handle_callback(MOD, MOD, 501, f"photo:{did}", now=NOW)

    row = drafts_store.get_draft(db, did)
    assert row["title"] == "Новый хук" and row["body"] == "Новое тело"


async def test_the_inbox_copy_is_removed_once_the_photo_is_in(db, tmp_path):
    """Скачанный кадр — перевалочный файл: после подстановки он лежит уже в
    медиа-каталоге, а копия в inbox только копит гигабайты на VPS."""
    did = draft(db)
    a = with_photos(admin(db, FakeBot()), tmp_path)
    await a.push_cards(now=NOW)

    await a.handle_photo_reply(MOD, MOD, 501, file_id="f")

    assert Path(drafts_store.get_draft(db, did)["media_path"]).exists()   # кадр доехал
    assert list((tmp_path / "inbox").glob("*")) == []                     # перевалка убрана


async def test_a_cancelled_photo_leaves_no_file_behind(db, tmp_path):
    did = draft(db)
    a = with_photos(admin(db, FakeBot()), tmp_path, verdict="foreign")
    await a.push_cards(now=NOW)
    await a.handle_photo_reply(MOD, MOD, 501, file_id="f")

    await a.handle_callback(MOD, MOD, 501, f"nophoto:{did}", now=NOW)

    assert list((tmp_path / "inbox").glob("*")) == []
