"""Карточка драфта в админ-боте (спека §7): текст, медиа, источники кластера,
категория, кнопки [Опубликовать] [Править] [Отклонить] [Отложить].

Карточка — это то, по чему модератор принимает решение за секунды, поэтому
всё важное («выдуманные факты», «мало фактуры», «чужой знак на медиа»,
«молния») должно быть видно в первом экране.
"""
import pytest

from tgtraff import drafts as drafts_store
from tgtraff.db import Database
from tgtraff.moderation.card import REASON_PREFIX, card_keyboard, card_text, reject_keyboard

CITY = "cherepovets"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def draft(db, **kw):
    base = dict(cluster_id=None, city=CITY, title="Такси вылетело на тротуар",
                body="Пострадали два человека.", category="dtp", urgency="normal",
                safety_verdict="safe", sources=["example_channel_7", "cher_life"],
                media_kind="photo", media_path="/m/a.jpg", district="Индустриальный район")
    return drafts_store.get_draft(db, drafts_store.save_draft(db, **{**base, **kw}))


def test_card_shows_hook_body_and_city(db):
    text = card_text(draft(db), city_name="Череповец")
    assert "Такси вылетело на тротуар" in text
    assert "Пострадали два человека." in text
    assert "Череповец" in text


def test_card_lists_the_cluster_sources(db):
    text = card_text(draft(db), city_name="Череповец")
    assert "example_channel_7" in text and "cher_life" in text


def test_card_shows_category_and_district(db):
    text = card_text(draft(db), city_name="Череповец")
    assert "ДТП" in text and "Индустриальный район" in text


def test_flash_is_visible_at_a_glance(db):
    assert "МОЛНИЯ" in card_text(draft(db, urgency="flash"), city_name="Череповец")


def test_manual_only_verdict_is_visible(db):
    text = card_text(draft(db, safety_verdict="manual_only",
                           safety_reasons=["военное"]), city_name="Череповец")
    assert "manual_only" in text and "военное" in text


def test_unsupported_facts_flag_is_loud(db):
    text = card_text(draft(db, flags=["unsupported_facts"],
                           notes="нет в источниках: 15:15"), city_name="Череповец")
    assert "выдум" in text.lower() or "нет в источниках" in text


def test_low_facts_flag_is_shown(db):
    assert "мало фактуры" in card_text(draft(db, flags=["low_facts"]),
                                       city_name="Череповец").lower()


def test_ugc_card_shows_the_author(db):
    text = card_text(draft(db, origin="ugc", flags=["ugc"], ugc_user_id=555,
                           ugc_username="reader"), city_name="Череповец")
    assert "UGC" in text and "reader" in text


def test_template_cover_is_named_as_such(db):
    text = card_text(draft(db, media_kind="template", media_reason="watermarked"),
                     city_name="Череповец")
    assert "обложка" in text.lower()


def test_card_fits_a_telegram_message(db):
    long_body = "Очень длинный текст. " * 400
    assert len(card_text(draft(db, body=long_body), city_name="Череповец")) <= 4096


def test_keyboard_has_the_four_buttons_of_the_spec(db):
    rows = card_keyboard(draft(db)).inline_keyboard
    labels = [b.text for row in rows for b in row]
    assert any("Опубликовать" in x for x in labels)
    assert any("Править" in x for x in labels)
    assert any("Отклонить" in x for x in labels)
    assert any("Отложить" in x for x in labels)


def test_every_button_carries_the_draft_id(db):
    row = draft(db)
    rows = card_keyboard(row).inline_keyboard
    assert all(str(row["id"]) in b.callback_data for r in rows for b in r)


def test_reject_asks_for_a_reason(db):
    """Решение ревью §28: причина отклонения определяет судьбу кластера."""
    labels = [b.text for row in reject_keyboard(draft(db)).inline_keyboard for b in row]
    assert any("не новость" in x.lower() for x in labels)
    assert any("фактур" in x.lower() for x in labels)
    data = [b.callback_data for row in reject_keyboard(draft(db)).inline_keyboard for b in row]
    assert any(d.startswith(REASON_PREFIX) for d in data)
