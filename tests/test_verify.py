"""Верификация записей `status: resolve` из sources.yaml (задача фаз 1-2 по спеке).

Логика поиска ленты/канала проверяется здесь, живые запросы — в scripts/verify_sources.py.
"""
from tgtraff.verify import (
    autodiscovered_feeds,
    feed_candidates,
    looks_like_feed,
    normalise_username,
    score_channel,
)


# ---- поиск RSS-ленты по адресу сайта ----

def test_candidates_cover_common_feed_paths():
    urls = feed_candidates("https://cherinfo.ru")
    assert "https://cherinfo.ru/rss" in urls
    assert "https://cherinfo.ru/feed" in urls
    assert any(u.endswith("rss.xml") for u in urls)


def test_candidates_keep_the_given_path():
    """У ведомств лента живёт под разделом: /deyatelnost/press-centr → .../press-centr/rss."""
    urls = feed_candidates("https://74.mchs.gov.ru/deyatelnost/press-centr")
    assert "https://74.mchs.gov.ru/deyatelnost/press-centr/rss" in urls
    assert "https://74.mchs.gov.ru/rss" in urls          # и корень сайта тоже пробуем


def test_candidates_do_not_repeat():
    assert len(feed_candidates("https://x.ru")) == len(set(feed_candidates("https://x.ru")))


def test_url_that_is_already_a_feed_is_tried_first():
    assert feed_candidates("https://cherinfo.ru/rss")[0] == "https://cherinfo.ru/rss"


def test_autodiscovery_finds_feed_link_in_html_head():
    html = """<html><head>
      <link rel="alternate" type="application/rss+xml" title="Новости" href="/news/rss/">
      <link rel="stylesheet" href="/style.css">
    </head></html>"""
    assert autodiscovered_feeds(html, "https://cherinfo.ru") == ["https://cherinfo.ru/news/rss/"]


def test_autodiscovery_handles_absolute_links_and_atom():
    html = '<link rel="alternate" type="application/atom+xml" href="https://x.ru/atom">'
    assert autodiscovered_feeds(html, "https://cherinfo.ru") == ["https://x.ru/atom"]


def test_autodiscovery_ignores_pages_without_feeds():
    assert autodiscovered_feeds("<html><body>нет ленты</body></html>", "https://x.ru") == []


def test_feed_detection_accepts_rss_and_atom():
    assert looks_like_feed(b"<?xml version='1.0'?><rss version='2.0'><channel/></rss>") is True
    assert looks_like_feed(b"<feed xmlns='http://www.w3.org/2005/Atom'><entry/></feed>") is True


def test_feed_detection_rejects_html_page():
    assert looks_like_feed(b"<!DOCTYPE html><html><body>404</body></html>") is False


# ---- поиск ТГ-канала по названию ----

def test_username_is_normalised_from_any_form():
    for value in ("@example_channel_7", "example_channel_7", "https://t.me/example_channel_7", "t.me/example_channel_7/123"):
        assert normalise_username(value) == "example_channel_7"


def test_private_invite_link_is_not_a_username():
    assert normalise_username("https://t.me/+AbCdEf123") is None
    assert normalise_username("https://t.me/joinchat/AbCdEf") is None


def test_exact_title_match_scores_highest():
    exact = score_channel("Магнитогорск 360", "Магнитогорск 360°")
    partial = score_channel("Магнитогорск новости", "Магнитогорск 360°")
    assert exact > partial


def test_channel_from_another_city_scores_zero():
    assert score_channel("Новокузнецк 360", "Магнитогорск 360°") == 0


def test_score_ignores_case_and_punctuation():
    assert score_channel("магнитогорск 360", "Магнитогорск 360°") == \
           score_channel("Магнитогорск 360", "Магнитогорск 360°")


# ---- ВК: groups.search сервисному ключу недоступен, ищем по кандидатам ----

from tgtraff.verify import screen_name_in_note, translit, vk_candidates  # noqa: E402


def test_transliterates_russian_query():
    assert translit("Типичный Новокузнецк") == "tipichnyy novokuznetsk"
    assert translit("Чёрное и белое") == "chernoe i beloe"


def test_note_may_already_contain_the_screen_name():
    """Стратегическая сессия иногда пишет адрес прямо в заметке — не искать же его заново."""
    assert screen_name_in_note("zapsib_slukh — локальные сплетни/UGC") == "zapsib_slukh"
    assert screen_name_in_note("vk.com/example_channel_12 — городской") == "example_channel_12"
    assert screen_name_in_note("просто заметка без адреса") is None


def test_note_screen_name_goes_first_among_candidates():
    cands = vk_candidates("Запсиб слух", note="zapsib_slukh — сплетни", city="novokuznetsk")
    assert cands[0] == "zapsib_slukh"


def test_candidates_include_transliterated_forms():
    cands = vk_candidates("Типичный Новокузнецк", city="novokuznetsk")
    assert "tipichnyy_novokuznetsk" in cands
    assert "typical_nvkz" in cands            # с подстановкой английского слова и сокращения


def test_candidates_use_city_abbreviation():
    cands = vk_candidates("ЧП Череповец", city="cherepovets")
    assert any(c.endswith("che") for c in cands)


def test_candidates_are_unique_and_look_like_screen_names():
    cands = vk_candidates("Чёрное и белое Магнитогорск", city="magnitogorsk")
    assert len(cands) == len(set(cands))
    assert all(c and c.replace("_", "").replace(".", "").isalnum() for c in cands)
