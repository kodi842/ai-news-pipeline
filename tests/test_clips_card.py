"""Карточка клипа в админ-боте (VIDEO-SPEC, фаза V1).

«Карточка клипа в админ-бот — видео-файл + сценарий + источник + площадки.
Кнопки [Публиковать] [Отклонить] [Перегенерить хук]. Автопаба клипов НЕТ.»

В V1 публикация ручная, значит карточка обязана давать всё, что нужно для
двух минут работы: сам файл, готовое описание для площадки и ответ на вопрос
«откуда это взялось».
"""
from types import SimpleNamespace

import pytest

from tgtraff.clips import card as clip_card, store
from tgtraff.clips.settings import ClipSettings
from tgtraff.db import Database
from tgtraff.moderation.bot import AdminBot
from tgtraff.publisher.channels import ChannelsConfig, CityChannels, TelegramChannel, VkGroup
from tgtraff.publisher.planner import Planner
from tgtraff.publisher.settings import ModerationSettings, parse_slots

CITY, TZ, MOD = "cherepovets", "Europe/Moscow", 777
SET = ClipSettings()


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
        self.calls = []
        self._next = 900

    async def _call(self, name, kw):
        self.calls.append((name, kw))
        self._next += 1
        return FakeMessage(self._next)

    async def send_message(self, **kw):
        return await self._call("send_message", kw)

    async def send_photo(self, **kw):
        return await self._call("send_photo", kw)

    async def send_video(self, **kw):
        return await self._call("send_video", kw)

    async def send_document(self, **kw):
        return await self._call("send_document", kw)

    async def edit_message_text(self, **kw):
        return await self._call("edit_message_text", kw)

    async def edit_message_caption(self, **kw):
        return await self._call("edit_message_caption", kw)

    async def edit_message_reply_markup(self, **kw):
        return await self._call("edit_message_reply_markup", kw)

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


def admin(db, bot=None):
    ch = channels()
    return AdminBot(db, bot or FakeBot(), ch, Planner(ch, {CITY: TZ}),
                    ModerationSettings(moderators=(MOD,)), city_names={CITY: "Череповец"},
                    clip_settings=SET)


def clip(db, tmp_path, status="ready", mode="video", flags=None, subtitles="whisper"):
    draft_id = db.conn.execute(
        """INSERT INTO drafts (city, title, body, category, safety_verdict, status, district)
           VALUES (?,?,?,?,'safe','published',?)""",
        (CITY, "Автобус № 21 столкнулся с фурой", "Утром на Ленина.", "dtp",
         "Зашекснинский")).lastrowid
    clip_id = store.save_clip(db, draft_id=draft_id, city=CITY, mode=mode, score=8,
                              score_reason="живое видео, узнаваемое место",
                              hook="Автобус смял фуру",
                              lines=["Утром на Ленина", "Движение перекрыто"],
                              cta="Подробности — в канале\n@example_channel_1",
                              flags=flags, media_kind="video" if mode == "video" else "photo",
                              media_ids=[1], source_key="example_channel_7", cost_usd=0.021)
    if status != "planned":
        store.claim_render(db, clip_id)
        path = tmp_path / f"clip{clip_id}.mp4"
        path.write_bytes(b"mp4" * 100)
        store.mark_rendered(db, clip_id, path=str(path), size=path.stat().st_size,
                            duration=26.0, subtitles=subtitles)
    return store.get_clip(db, clip_id)


# ---------- текст карточки ----------

def test_card_shows_the_script(db, tmp_path):
    row = clip(db, tmp_path)
    text = clip_card.card_text(row, city_name="Череповец", settings=SET)
    assert "Автобус смял фуру" in text
    assert "Утром на Ленина" in text
    assert "Движение перекрыто" in text


def test_card_shows_where_to_publish(db, tmp_path):
    """В V1 модератор публикует руками — карточка должна сказать куда."""
    row = clip(db, tmp_path)
    text = clip_card.card_text(row, city_name="Череповец", settings=SET)
    assert "VK Клипы" in text


def test_card_shows_source_score_and_length(db, tmp_path):
    row = clip(db, tmp_path)
    text = clip_card.card_text(row, city_name="Череповец", settings=SET)
    assert "example_channel_7" in text
    assert "8" in text
    assert "26" in text                       # длительность
    assert "субтитры" in text.lower()


def test_card_says_when_the_watermark_was_not_checked(db, tmp_path):
    """Флаг media_unverified — единственная причина смотреть кадры глазами
    особенно внимательно; спрятать его в базе нельзя."""
    row = clip(db, tmp_path, flags=["media_unverified"])
    text = clip_card.card_text(row, city_name="Череповец", settings=SET)
    assert "не проверен" in text.lower()


def test_card_warns_about_unsupported_facts(db, tmp_path):
    row = clip(db, tmp_path, flags=["unsupported_facts"])
    assert "факт" in clip_card.card_text(row, city_name="Череповец", settings=SET).lower()


def test_card_marks_a_silent_clip(db, tmp_path):
    row = clip(db, tmp_path, subtitles="lines")
    text = clip_card.card_text(row, city_name="Череповец", settings=SET)
    assert "без озвучки" in text.lower() or "без голоса" in text.lower()


def test_description_for_the_platform_is_ready_to_paste(db, tmp_path):
    """Две минуты на клип — это когда описание не надо придумывать заново."""
    row = clip(db, tmp_path)
    text = clip_card.description(row, city_name="Череповец", username="example_channel_1")
    assert "Автобус смял фуру" in text
    assert "@example_channel_1" in text or "t.me/example_channel_1" in text


def test_keyboard_has_exactly_the_three_spec_buttons(db, tmp_path):
    row = clip(db, tmp_path)
    labels = [b.text for line in clip_card.card_keyboard(row).inline_keyboard for b in line]
    assert any("убликов" in l for l in labels)
    assert any("тклон" in l for l in labels)
    assert any("хук" in l.lower() for l in labels)
    assert len(labels) == 3


# ---------- бот ----------

async def test_ready_clip_is_pushed_as_a_video_with_buttons(db, tmp_path):
    row = clip(db, tmp_path)
    bot = FakeBot()
    assert await admin(db, bot).push_clip_cards() == 1
    name, kw = bot.calls[0]
    assert name == "send_video"
    assert "Автобус смял фуру" in kw["caption"]
    assert kw["reply_markup"].inline_keyboard
    assert store.get_clip(db, row["id"])["status"] == "sent"


async def test_clip_without_a_file_is_not_pushed(db, tmp_path):
    row = clip(db, tmp_path)
    db.conn.execute("UPDATE clips SET file_path='/нет/файла.mp4' WHERE id=?", (row["id"],))
    bot = FakeBot()
    assert await admin(db, bot).push_clip_cards() == 0
    assert bot.calls == []


async def test_publish_button_records_the_decision_and_gives_the_text(db, tmp_path):
    row = clip(db, tmp_path)
    bot = FakeBot()
    a = admin(db, bot)
    await a.push_clip_cards()
    answer = await a.handle_callback(MOD, chat_id=MOD, message_id=901,
                                     data=f"{clip_card.CLIP_PUBLISH}:{row['id']}")
    assert "выклад" in answer.lower() or "готов" in answer.lower()
    saved = store.get_clip(db, row["id"])
    assert saved["status"] == "published"
    assert saved["moderator_id"] == MOD
    # Описание для площадки бот присылает сразу — копировать из карточки руками
    # неудобно, а именно этим модератор и занят.
    assert any("@example_channel_1" in t for t in bot.texts()[1:])


async def test_reject_button_closes_the_card(db, tmp_path):
    row = clip(db, tmp_path)
    bot = FakeBot()
    a = admin(db, bot)
    await a.push_clip_cards()
    await a.handle_callback(MOD, chat_id=MOD, message_id=901,
                            data=f"{clip_card.CLIP_REJECT}:{row['id']}")
    assert store.get_clip(db, row["id"])["status"] == "rejected"
    assert "edit_message_caption" in bot.names() or "edit_message_text" in bot.names()


async def test_rehook_button_sends_the_clip_back_to_the_factory(db, tmp_path):
    """Перегенерить хук — работа платной модели, и она живёт в контейнере
    завода. Бот только ставит отметку, а не ходит в API из чата модерации."""
    row = clip(db, tmp_path)
    bot = FakeBot()
    a = admin(db, bot)
    await a.push_clip_cards()
    answer = await a.handle_callback(MOD, chat_id=MOD, message_id=901,
                                     data=f"{clip_card.CLIP_REHOOK}:{row['id']}")
    assert store.get_clip(db, row["id"])["status"] == "rehook"
    assert "хук" in answer.lower()


async def test_stranger_cannot_touch_clips(db, tmp_path):
    row = clip(db, tmp_path)
    a = admin(db)
    await a.handle_callback(12345, chat_id=1, message_id=901,
                            data=f"{clip_card.CLIP_PUBLISH}:{row['id']}")
    assert store.get_clip(db, row["id"])["status"] == "ready"


async def test_clip_buttons_do_not_collide_with_draft_buttons(db, tmp_path):
    """Кнопки клипов и кнопки драфтов живут в одном callback_data: id клипа
    не должен приниматься за id драфта."""
    row = clip(db, tmp_path)
    a = admin(db)
    for prefix in (clip_card.CLIP_PUBLISH, clip_card.CLIP_REJECT, clip_card.CLIP_REHOOK):
        assert prefix not in ("pub", "rej", "edit", "defer", "now", "del")
    answer = await a.handle_callback(MOD, chat_id=MOD, message_id=1,
                                     data=f"{clip_card.CLIP_PUBLISH}:{row['id']}")
    assert "не понял" not in answer.lower()


def test_card_warns_when_the_outro_has_no_channel_handle(db, tmp_path):
    """Клип без @ника в аутро публиковать бессмысленно — карточка обязана
    сказать это раньше, чем модератор нажмёт «Публиковать»."""
    row = clip(db, tmp_path, flags=["no_username"])
    text = clip_card.card_text(row, city_name="Череповец", settings=SET)
    assert "@" in text and ("нет" in text.lower() or "без" in text.lower())
