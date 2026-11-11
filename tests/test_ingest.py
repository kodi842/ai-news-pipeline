"""Нормализация сообщения источника в запись raw_posts (без сети)."""
from datetime import datetime, timedelta, timezone

import pytest
from telethon.tl.types import (
    Document,
    DocumentAttributeFilename,
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaPoll,
    MessageMediaWebPage,
    PeerChannel,
    Photo,
)

from tgtraff.collector.ingest import media_kind, mentions_city, normalize_message
from tgtraff.config import SourceSpec

TG = SourceSpec(city="cherepovets", source_type="tg", key="example_channel_7")
REGIONAL = SourceSpec(city="magnitogorsk", source_type="tg_regional", key="news_74ru", city_filter="магнитогорск")


def msg(text="", mid=100, media=None, **kw):
    return Message(
        id=mid,
        peer_id=PeerChannel(channel_id=1234),
        date=kw.pop("date", datetime(2026, 8, 28, 7, 30, tzinfo=timezone.utc)),
        message=text,
        media=media,
        **kw,
    )


def photo():
    return MessageMediaPhoto(photo=Photo(id=1, access_hash=1, file_reference=b"", date=datetime.now(timezone.utc),
                                         sizes=[], dc_id=2))


def document(mime="video/mp4", name="clip.mp4"):
    return MessageMediaDocument(document=Document(
        id=1, access_hash=1, file_reference=b"", date=datetime.now(timezone.utc), mime_type=mime, size=100,
        dc_id=2, attributes=[DocumentAttributeFilename(file_name=name)]))


# ---- нормализация ----

def test_maps_core_fields(tmp_path):
    p = normalize_message(msg("Горел склад на Северном", mid=512, views=340), TG)
    assert p.external_id == "512"
    assert p.text == "Горел склад на Северном"
    assert p.published_at == "2026-08-28T07:30:00+00:00"
    assert p.url == "https://t.me/example_channel_7/512"
    assert p.views == 340
    assert p.filtered_out is False


def test_album_keeps_grouped_id():
    assert normalize_message(msg("а", grouped_id=999), TG).grouped_id == "999"


def test_edited_message_keeps_edit_date():
    edited = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)
    assert normalize_message(msg("а", edit_date=edited), TG).edited_at == "2026-08-28T08:00:00+00:00"


def test_naive_date_is_treated_as_utc():
    p = normalize_message(msg("а", date=datetime(2026, 8, 28, 7, 30)), TG)
    assert p.published_at.endswith("+00:00")


# ---- медиа ----

@pytest.mark.parametrize("media,expected", [
    (photo(), "photo"),
    (document("video/mp4"), "video"),
    (document("audio/ogg", "voice.ogg"), "audio"),
    (document("application/pdf", "doc.pdf"), "document"),
    (None, None),
    (MessageMediaWebPage(webpage=None), None),   # превью ссылки — не медиа поста
    (MessageMediaPoll(poll=None, results=None), None),
])
def test_media_kind_detection(media, expected):
    assert media_kind(msg("текст", media=media)) == expected


def test_post_with_photo_is_marked_pending():
    p = normalize_message(msg("пожар", media=photo()), TG)
    assert p.has_media is True
    assert p.media_state == "pending"
    assert [m.kind for m in p.media] == ["photo"]


def test_post_without_media_has_no_media_rows():
    p = normalize_message(msg("просто текст"), TG)
    assert p.has_media is False and p.media == [] and p.media_state == "none"


# ---- фильтр по городу для областных источников ----

@pytest.mark.parametrize("text,expected", [
    ("В Магнитогорске сгорел автобус", True),
    ("магнитогорск: ДТП на Ленина", True),
    ("Жители МАГНИТОГОРСКА жалуются", True),
    ("В Челябинске открыли развязку", False),
    ("", False),
])
def test_mentions_city(text, expected):
    assert mentions_city(text, "магнитогорск") is expected


def test_regional_post_without_city_is_filtered_and_media_not_queued():
    p = normalize_message(msg("В Челябинске открыли развязку", media=photo()), REGIONAL)
    assert p.filtered_out is True
    assert p.filter_reason == "city_filter:магнитогорск"
    assert p.media == []                # чужое медиа не качаем
    assert p.media_state == "skipped"


def test_regional_post_with_city_passes():
    p = normalize_message(msg("В Магнитогорске ДТП", media=photo()), REGIONAL)
    assert p.filtered_out is False
    assert [m.kind for m in p.media] == ["photo"]


def test_city_filter_applies_to_caption_of_media_post():
    assert normalize_message(msg("", media=photo()), REGIONAL).filtered_out is True


def test_plain_source_has_no_city_filter():
    assert normalize_message(msg("В Челябинске развязка"), TG).filtered_out is False
