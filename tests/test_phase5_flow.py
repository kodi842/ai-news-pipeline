"""Полный цикл приёмки фазы 5 (спека, критерии фазы 5):

    пост в источнике → дедуп → драфт → карточка в админ-боте → кнопка
        → очередь со слотом → публикация в канал → карта draft→message_id

Внешнего здесь нет ничего: модель эмбеддингов, Haiku и Bot API — двойники.
Проверяется склейка всех узлов фазы, а не отдельная функция.
"""
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from tgtraff import drafts as drafts_store
from tgtraff.config import SourceSpec
from tgtraff.db import Database
from tgtraff.dedup.cluster import Deduplicator
from tgtraff.dedup.settings import DedupSettings
from tgtraff.moderation.bot import AdminBot
from tgtraff.normalize import normalized_feed
from tgtraff.pipeline import Phase4Pipeline, PipelineSettings
from tgtraff.publisher import store
from tgtraff.publisher.channels import ChannelsConfig, CityChannels, TelegramChannel, VkGroup
from tgtraff.publisher.delivery import TelegramDelivery
from tgtraff.publisher.planner import Planner
from tgtraff.publisher.runner import Publisher
from tgtraff.publisher.settings import ModerationSettings, PublisherSettings, parse_slots

CITY, TZ, MOD = "cherepovets", "Europe/Moscow", 777
NEWS = ("На Октябрьском мосту столкнулись автобус и легковушка. "
        "Пострадал водитель легковушки, его увезли в больницу. Движение затруднено.")
# Нейтральная новость для §59: ни жертв, ни имён, ни криминала.
UTILITY = ("Холодную воду в Зашекснинском отключат в четверг с 09:00 до 18:00. "
           "Коммунальщики меняют участок трубы на улице Наседкина.")
UTILITY_DRAFT = {"title": "Воду в Зашекснинском отключат в четверг",
                 "body": "Холодную воду отключат с 09:00 до 18:00. Коммунальщики "
                         "меняют участок трубы на улице Наседкина.",
                 "category": "utility", "urgency": "normal", "district": None,
                 "low_facts": False, "note": ""}
EVENING = datetime(2026, 9, 4, 15, 30, tzinfo=timezone.utc)     # 18:30 локального — слот
NIGHT = datetime(2026, 9, 4, 22, 30, tzinfo=timezone.utc)       # 01:30 локального — вне слотов


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


# ---------- двойники внешнего мира ----------

class FakeEmbedder:
    name, dim = "fake", 8

    def __init__(self):
        self._slots = {}

    def encode(self, texts):
        out = []
        for t in texts:
            vec = np.zeros(self.dim, dtype=np.float32)
            key = t[:40]
            if "мосту столкнулись" in t:                       # одна и та же новость
                vec[0], vec[1] = math.cos(0), math.sin(0)
            else:
                vec[self._slots.setdefault(key, 2 + len(self._slots) % 6)] = 1.0
            out.append(vec)
        return np.vstack(out) if out else np.zeros((0, self.dim))


class Usage:
    input_tokens = output_tokens = cache_read = cache_write = 0
    cost_usd = 0.0002


class FakeLlm:
    """Отвечает по назначению запроса, как настоящий клиент фазы 4."""

    def __init__(self, urgency="normal", draft=None):
        self.on_usage = None
        self.urgency = urgency
        self.draft = draft
        self.settings = type("S", (), {"model": "claude-haiku-4-5"})()

    def ask_json(self, system, user, purpose="other", **kw):
        rewrite = dict(self.draft) if self.draft else {
            "title": "Автобус и легковушка столкнулись на Октябрьском мосту",
            "body": "Пострадал водитель легковушки, его увезли в больницу. "
                    "Движение затруднено.",
            "category": "dtp", "district": "Индустриальный район",
            "low_facts": False, "note": ""}
        rewrite["urgency"] = self.urgency
        replies = {
            "relevance": {"verdict": "news"},
            "rewrite": rewrite,
            "safety": {"verdict": "safe", "categories": [], "reason": ""},
        }
        if self.on_usage:
            self.on_usage(purpose, "claude-haiku-4-5", Usage())
        return replies[purpose], type("R", (), {"model": "claude-haiku-4-5",
                                                "cost_usd": Usage.cost_usd})()


class FakeTelegram:
    """Двойник aiogram.Bot: и для карточек модератора, и для постинга в канал."""

    def __init__(self):
        self.calls = []
        self._next = 100

    async def _call(self, name, kw):
        self.calls.append((name, kw))
        self._next += 1
        return type("M", (), {"message_id": self._next})()

    def __getattr__(self, name):
        if name.startswith(("send_", "edit_", "delete_")):
            async def method(**kw):
                return await self._call(name, kw)
            return method
        raise AttributeError(name)

    def to_channel(self):
        return [kw for n, kw in self.calls
                if n.startswith("send_") and str(kw.get("chat_id")) == "-1001test"]


def channels():
    city = CityChannels(city=CITY, deep_link="che",
                        tg=TelegramChannel(chat_id="-1001test", username="chp_cher_test",
                                           title="ЧП Череповец"),
                        vk=VkGroup(), slots=parse_slots(["07:00-09:00", "18:00-22:00"]),
                        min_gap_minutes=20, max_gap_minutes=40, daily_limit=18)
    return ChannelsConfig(mode="test", ugc_bot="example_channel_3", cities={CITY: city})


def source_post(db, key, text, at, source_type="tg", media_path=None):
    sid = db.upsert_source(SourceSpec(city=CITY, source_type=source_type, key=key))
    res = db.insert_raw_post(source_id=sid, source_type=source_type,
                             external_id=f"{key}-{at}", published_at=at, text=text,
                             has_media=1 if media_path else 0,
                             media_state="pending" if media_path else "none")
    if media_path:
        mid = db.add_media(res.id, 0, "photo")
        db.conn.execute("UPDATE raw_media SET status='done', file_path=?, watermark='none' "
                        "WHERE id=?", (str(media_path), mid))
    return res.id


class Stage:
    """Собранная фаза 5: дедуп → драфты → админ-бот → publisher."""

    def __init__(self, db, urgency="normal", tmp_path=None, draft=None, autopublish=()):
        self.db = db
        self.ch = channels()
        self.bot = FakeTelegram()
        self.planner = Planner(self.ch, {CITY: TZ})
        self.deliveries = {"tg": TelegramDelivery(self.bot)}
        self.admin = AdminBot(db, self.bot, self.ch, self.planner,
                              ModerationSettings(moderators=(MOD,)),
                              city_names={CITY: "Череповец"}, deliveries=self.deliveries,
                              autopublish_categories=autopublish)
        self.publisher = Publisher(db, self.ch, self.deliveries, PublisherSettings())
        self.publisher.on_published = self.admin.announce_published
        self.pipeline = Phase4Pipeline(
            db, FakeLlm(urgency, draft=draft),
            settings=PipelineSettings(cover_dir=str(tmp_path / "covers") if tmp_path else "."),
            city_names={CITY: "Череповец"})

    def dedup(self):
        feed = list(reversed(normalized_feed(self.db, city=CITY, limit=100)))
        return Deduplicator(self.db, FakeEmbedder(), DedupSettings()).process_feed(feed)


# ---------- сам цикл ----------

async def test_post_in_a_source_becomes_a_published_channel_post(db, tmp_path):
    photo = tmp_path / "dtp.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    source_post(db, "example_channel_7", NEWS, "2026-09-04T14:00:00+00:00", media_path=photo)
    source_post(db, "cher_life", NEWS + " Подробности уточняются.",
                "2026-09-04T14:05:00+00:00", source_type="vk")

    stage = Stage(db, tmp_path=tmp_path)

    # 1. дедуп: две публикации — одна новость
    stage.dedup()
    cluster_id = db.conn.execute("SELECT id FROM clusters").fetchone()["id"]
    assert db.conn.execute("SELECT COUNT(*) c FROM cluster_members WHERE cluster_id=?",
                           (cluster_id,)).fetchone()["c"] == 2

    # 2. конвейер фазы 4: кластер → драфт со статусом new (автопубликации нет)
    assert stage.pipeline.run_once().drafts == 1
    draft = drafts_store.draft_for_cluster(db, cluster_id)
    assert draft["status"] == "new" and draft["safety_verdict"] == "safe"

    # 3. карточка модератору: обычная новость не пушится (§55) — её тянут из очереди
    assert await stage.admin.push_cards(now=EVENING) == 0
    assert await stage.admin.handle_queue(MOD, MOD, args='1') == 1
    card = stage.bot.calls[0]
    assert str(card[1]["chat_id"]) == str(MOD)
    assert "Октябрьском мосту" in (card[1].get("caption") or card[1].get("text"))

    # 4. кнопка «Опубликовать» → слот
    card_message_id = drafts_store.get_draft(db, draft["id"])["card_message_id"]
    await stage.admin.handle_callback(MOD, MOD, card_message_id, f"pub:{draft['id']}",
                                      now=EVENING)
    queue_row = store.queue_row_for_draft(db, draft["id"])
    assert queue_row["status"] == "queued"
    assert store.parse(queue_row["scheduled_at"]) == EVENING       # мы внутри слота

    # 5. publisher: пост в канале и карта draft → message_id
    outcomes = await stage.publisher.tick(now=EVENING)
    assert [o.status for o in outcomes] == ["published"]
    sent = stage.bot.to_channel()
    assert len(sent) == 1
    text = sent[0].get("caption") or sent[0].get("text")
    assert "🚗 <b>Автобус и легковушка" in text
    assert "#Индустриальный" in text and "chp_cher_test" in text
    assert sent[0]["reply_markup"].inline_keyboard[0][0].url.endswith("?start=che")

    publication = store.publications(db, draft["id"])[0]
    assert publication["message_id"] and publication["channel"] == "-1001test"
    assert drafts_store.get_draft(db, draft["id"])["status"] == "published"


async def test_night_news_waits_for_the_morning_slot(db, tmp_path):
    source_post(db, "example_channel_7", NEWS, "2026-09-04T22:00:00+00:00")
    stage = Stage(db, tmp_path=tmp_path)
    stage.dedup()
    stage.pipeline.run_once()
    draft = db.conn.execute("SELECT * FROM drafts").fetchone()

    await stage.admin.handle_queue(MOD, MOD, args='1')
    await stage.admin.handle_callback(MOD, MOD, drafts_store.get_draft(
        db, draft["id"])["card_message_id"], f"pub:{draft['id']}", now=NIGHT)

    when = store.parse(store.queue_row_for_draft(db, draft["id"])["scheduled_at"])
    assert when == datetime(2026, 9, 5, 4, 0, tzinfo=timezone.utc)   # 07:00 локального
    assert await stage.publisher.tick(now=NIGHT) == []               # раньше слота — молчим
    assert await stage.publisher.tick(now=when) != []


async def test_flash_goes_out_immediately_at_night(db, tmp_path):
    """Спека §8: «молния» публикуется вне сетки сразу после модерации."""
    source_post(db, "example_channel_7", NEWS, "2026-09-04T22:00:00+00:00")
    stage = Stage(db, urgency="flash", tmp_path=tmp_path)
    stage.dedup()
    stage.pipeline.run_once()
    draft = db.conn.execute("SELECT * FROM drafts").fetchone()

    await stage.admin.push_cards(now=NIGHT)
    answer = await stage.admin.handle_callback(
        MOD, MOD, drafts_store.get_draft(db, draft["id"])["card_message_id"],
        f"pub:{draft['id']}", now=NIGHT)
    assert "сейчас" in answer.lower()
    assert await stage.publisher.tick(now=NIGHT + timedelta(seconds=1))
    assert store.publications(db, draft["id"])


async def test_moderator_edit_reaches_the_channel(db, tmp_path):
    source_post(db, "example_channel_7", NEWS, "2026-09-04T14:00:00+00:00")
    stage = Stage(db, tmp_path=tmp_path)
    stage.dedup()
    stage.pipeline.run_once()
    draft = db.conn.execute("SELECT * FROM drafts").fetchone()
    await stage.admin.handle_queue(MOD, MOD, args='1')
    card_id = drafts_store.get_draft(db, draft["id"])["card_message_id"]

    # правка до публикации
    await stage.admin.handle_reply(MOD, MOD, card_id, "Новый хук\nНовое тело поста.")
    await stage.admin.handle_callback(MOD, MOD, card_id, f"pub:{draft['id']}", now=EVENING)
    await stage.publisher.tick(now=EVENING)
    text = stage.bot.to_channel()[0].get("caption") or stage.bot.to_channel()[0].get("text")
    assert "Новый хук" in text and "Новое тело поста." in text

    # правка после публикации — уходит в канал (пост с обложкой правится подписью)
    await stage.admin.handle_reply(MOD, MOD, card_id, "Исправленный хук")
    edits = [kw for n, kw in stage.bot.calls
             if n in ("edit_message_text", "edit_message_caption")
             and str(kw.get("chat_id")) == "-1001test"]
    assert edits
    assert "Исправленный хук" in (edits[-1].get("caption") or edits[-1].get("text"))


async def test_rejected_draft_never_reaches_the_channel(db, tmp_path):
    source_post(db, "example_channel_7", NEWS, "2026-09-04T14:00:00+00:00")
    stage = Stage(db, tmp_path=tmp_path)
    stage.dedup()
    stage.pipeline.run_once()
    draft = db.conn.execute("SELECT * FROM drafts").fetchone()
    await stage.admin.handle_queue(MOD, MOD, args='1')
    card_id = drafts_store.get_draft(db, draft["id"])["card_message_id"]

    await stage.admin.handle_callback(MOD, MOD, card_id, f"rej:{draft['id']}", now=EVENING)
    await stage.admin.handle_callback(MOD, MOD, card_id, f"why:{draft['id']}:not_news",
                                      now=EVENING)
    assert await stage.publisher.tick(now=EVENING) == []
    assert stage.bot.to_channel() == []
    assert drafts_store.get_draft(db, draft["id"])["status"] == "rejected"


async def test_watermarked_media_never_goes_to_the_channel(db, tmp_path):
    """Критерий приёмки: у кластера все медиа с чужим знаком → выходит своя обложка."""
    foreign = tmp_path / "foreign.jpg"
    foreign.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    pid = source_post(db, "example_channel_7", NEWS, "2026-09-04T14:00:00+00:00", media_path=foreign)
    db.conn.execute("UPDATE raw_media SET watermark='present', watermark_owner='chp_cher' "
                    "WHERE raw_post_id=?", (pid,))
    stage = Stage(db, tmp_path=tmp_path)
    stage.dedup()
    stage.pipeline.run_once()
    draft = db.conn.execute("SELECT * FROM drafts").fetchone()
    assert draft["media_kind"] == "template" and draft["media_reason"] == "watermarked"

    await stage.admin.handle_queue(MOD, MOD, args='1')
    await stage.admin.handle_callback(MOD, MOD, drafts_store.get_draft(
        db, draft["id"])["card_message_id"], f"pub:{draft['id']}", now=EVENING)
    await stage.publisher.tick(now=EVENING)

    sent = stage.bot.to_channel()[0]
    assert str(foreign) not in str(sent)
    assert "covers" in str(sent.get("photo").path)          # ушла наша карточка


# ---------- §59: путь в канал без человека ----------

AUTO = ("weather", "utility", "city", "other")


async def test_neutral_news_reaches_the_channel_without_a_moderator(db, tmp_path):
    """§59: шесть условий сошлись — пост уходит в канал сам, карточки не было."""
    source_post(db, "example_channel_7", UTILITY, "2026-09-04T14:00:00+00:00")
    stage = Stage(db, tmp_path=tmp_path, draft=UTILITY_DRAFT, autopublish=AUTO)
    stage.dedup()
    assert stage.pipeline.run_once().drafts == 1
    draft = db.conn.execute("SELECT * FROM drafts").fetchone()
    assert draft["status"] == "new" and drafts_store.decode_list(draft["flags"]) == []

    assert await stage.admin.autopublish(now=EVENING) == 1
    assert stage.bot.calls == []                      # карточки модератору не приходило

    assert [o.status for o in await stage.publisher.tick(now=EVENING)] == ["published"]
    assert len(stage.bot.to_channel()) == 1
    row = drafts_store.get_draft(db, draft["id"])
    assert row["status"] == "published"
    assert row["moderator_id"] is None and row["autopublished_at"]


async def test_the_same_news_waits_for_a_button_when_autopublish_is_off(db, tmp_path):
    """Kill-switch §59: пустой список — и та же новость ждёт человека в пуле."""
    source_post(db, "example_channel_7", UTILITY, "2026-09-04T14:00:00+00:00")
    stage = Stage(db, tmp_path=tmp_path, draft=UTILITY_DRAFT)
    stage.dedup()
    stage.pipeline.run_once()

    assert await stage.admin.autopublish(now=EVENING) == 0
    assert await stage.publisher.tick(now=EVENING) == []
    assert stage.bot.to_channel() == []
    assert db.conn.execute("SELECT status FROM drafts").fetchone()["status"] == "new"
