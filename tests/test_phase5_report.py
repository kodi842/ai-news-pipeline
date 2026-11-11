"""Предпросмотр фазы 5: как выглядят карточка модератора и пост в канале.

Отчёт собирается из живых драфтов и нужен ревью: формат-шаблоны и карточку
проще проверить глазами, чем по коду.
"""
import pytest

from tgtraff import drafts as drafts_store
from tgtraff.db import Database
from tgtraff.moderation.report import render_preview
from tgtraff.publisher.channels import ChannelsConfig, CityChannels, TelegramChannel, VkGroup
from tgtraff.publisher.settings import parse_slots

CITY = "cherepovets"


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


def draft(db, **kw):
    base = dict(cluster_id=None, city=CITY, title="Такси вылетело на тротуар",
                body="Пострадали два человека.", category="dtp", urgency="normal",
                safety_verdict="safe", sources=["example_channel_7"], district="Индустриальный район")
    return drafts_store.save_draft(db, **{**base, **kw})


def test_preview_shows_both_the_card_and_the_post(db):
    draft(db)
    text = render_preview(db, channels(), {CITY: "Череповец"}, limit=5)
    assert "карточка модератора" in text.lower()
    assert "пост в канале" in text.lower()
    assert "#Индустриальный" in text


def test_preview_covers_several_drafts(db):
    for i in range(3):
        draft(db, title=f"Новость {i}")
    text = render_preview(db, channels(), {CITY: "Череповец"}, limit=2)
    assert "Новость 0" in text and "Новость 1" in text and "Новость 2" not in text


def test_preview_without_drafts_says_so(db):
    assert "нет" in render_preview(db, channels(), {}, limit=5).lower()


def test_preview_skips_cities_without_a_channel(db):
    draft(db, city="magnitogorsk")
    text = render_preview(db, channels(), {}, limit=5)
    assert "канал не настроен" in text.lower()
