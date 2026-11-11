"""Активные ссылки на источники в карточке модератора (решение ревью §61.2).

Модератор проверяет фактуру по первоисточнику, и до этого решения путь к нему
был ручной: скопировать имя канала, найти его поиском, отлистать до нужного
поста. Здесь имя источника в карточке становится ссылкой прямо на публикацию.

Граница, которую эти тесты и стерегут: ссылки живут ТОЛЬКО в карточке.
В публикуемый пост они не попадают — стайлгайд §6 запрещает уводить читателя
из нашего поста в чужой канал.
"""
import pytest

from tgtraff import drafts as drafts_store
from tgtraff.config import SourceSpec
from tgtraff.db import Database
from tgtraff.moderation import actions
from tgtraff.moderation.card import card_text

CITY = "cherepovets"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def source(db, key, source_type="tg", peer_id=None):
    sid = db.upsert_source(SourceSpec(city=CITY, source_type=source_type, key=key))
    if peer_id:
        db.set_source_peer(sid, peer_id)
    return sid


def post(db, source_id, external_id, url, at="2026-09-01T10:00:00+00:00", source_type="tg"):
    return db.insert_raw_post(source_id=source_id, source_type=source_type,
                              external_id=str(external_id), published_at=at,
                              text="Пожар на Наседкина", url=url).id


def cluster(db, *post_ids):
    cur = db.conn.execute(
        "INSERT INTO clusters (city, status, lead_raw_post_id, size, first_seen_at, last_seen_at) "
        "VALUES (?, 'open', ?, ?, '2026-09-01T10:00:00+00:00', '2026-09-01T10:00:00+00:00')",
        (CITY, post_ids[0], len(post_ids)))
    for pid in post_ids:
        db.conn.execute(
            "INSERT INTO cluster_members (cluster_id, raw_post_id, reason) VALUES (?,?,'new')",
            (cur.lastrowid, pid))
    return cur.lastrowid


def draft(db, cluster_id=None, **kw):
    base = dict(cluster_id=cluster_id, city=CITY, title="Пожар на Наседкина",
                body="Горит расселённый дом.", category="fire", urgency="normal",
                safety_verdict="safe", sources=["example_channel_7"])
    return drafts_store.get_draft(db, drafts_store.save_draft(db, **{**base, **kw}))


# --- сбор ссылок из базы -----------------------------------------------------

def test_public_telegram_source_links_to_the_message(db):
    sid = source(db, "example_channel_7")
    cid = cluster(db, post(db, sid, 4242, "https://t.me/example_channel_7/4242"))

    links = actions.source_links(db, cid)

    assert [(l.name, l.url) for l in links] == [("example_channel_7", "https://t.me/example_channel_7/4242")]
    assert links[0].private is False


def test_vk_and_rss_sources_keep_their_own_urls(db):
    vk = post(db, source(db, "cherepovets_life", "vk"), "-124_337",
              "https://vk.com/wall-124_337", source_type="vk")
    rss = post(db, source(db, "https://example_channel_7.ru/feed", "rss"), "guid-9",
               "https://example_channel_7.ru/news/9", source_type="rss")

    links = {l.name: l.url for l in actions.source_links(db, cluster(db, vk, rss))}

    assert links["cherepovets_life"] == "https://vk.com/wall-124_337"
    assert links["https://example_channel_7.ru/feed"] == "https://example_channel_7.ru/news/9"


def test_private_telegram_channel_falls_back_to_the_internal_form(db):
    """У приватного канала username'а нет, и url коллектор не собрал. Ссылку
    строим внутренней формой t.me/c/<peer>/<msg> — она откроется только тому,
    кто в канал вступил, поэтому помечается как приватная."""
    sid = source(db, "?Череповец 360°", peer_id=1234567890)
    cid = cluster(db, post(db, sid, 77, None))

    link = actions.source_links(db, cid)[0]

    assert link.url == "https://t.me/c/1234567890/77"
    assert link.private is True


def test_source_without_url_and_without_peer_stays_a_plain_name(db):
    cid = cluster(db, post(db, source(db, "no_peer"), 5, None))

    assert actions.source_links(db, cid) == [actions.SourceLink("no_peer", None, False)]


def test_a_source_that_posted_twice_is_listed_once(db):
    sid = source(db, "example_channel_7")
    cid = cluster(db,
                  post(db, sid, 10, "https://t.me/example_channel_7/10", at="2026-09-01T10:00:00+00:00"),
                  post(db, sid, 11, "https://t.me/example_channel_7/11", at="2026-09-01T11:00:00+00:00"))

    links = actions.source_links(db, cid)

    assert len(links) == 1
    assert links[0].url == "https://t.me/example_channel_7/10"      # первая публикация


def test_draft_without_a_cluster_has_no_links(db):
    """UGC приходит не из кластера — ссылаться не на что, и это не ошибка."""
    assert actions.source_links(db, None) == []


# --- как это выглядит в карточке ---------------------------------------------

def test_card_makes_the_source_name_clickable(db):
    row = draft(db)
    links = [actions.SourceLink("example_channel_7", "https://t.me/example_channel_7/4242")]

    text = card_text(row, city_name="Череповец", links=links)

    assert '<a href="https://t.me/example_channel_7/4242">example_channel_7</a>' in text


def test_card_shows_no_raw_urls_next_to_the_anchor(db):
    """Компактно (§61.2): ссылка-анкор на имени, сырых URL в тексте нет."""
    links = [actions.SourceLink("example_channel_7", "https://t.me/example_channel_7/4242")]

    line = next(l for l in card_text(draft(db), links=links).splitlines()
                if "источник" in l)

    assert line.count("https://t.me/example_channel_7/4242") == 1   # только внутри href


def test_card_warns_that_a_private_link_opens_only_for_members(db):
    links = [actions.SourceLink("Череповец 360°", "https://t.me/c/123/77", private=True)]

    text = card_text(draft(db), links=links)

    assert "только участник" in text.lower()


def test_card_without_links_still_lists_the_source_names(db):
    """Ссылок нет (старый драфт, источник без url) — карточка прежняя."""
    text = card_text(draft(db), city_name="Череповец")

    assert "example_channel_7" in text and "<a href" not in text


def test_source_name_is_escaped_inside_the_anchor(db):
    """Имя источника — чужой текст: разметку карточки оно ломать не должно."""
    links = [actions.SourceLink('<b>x</b>&', "https://t.me/x/1")]

    text = card_text(draft(db), links=links)

    assert "&lt;b&gt;x&lt;/b&gt;&amp;</a>" in text


# --- граница: в пост ссылки не идут ------------------------------------------

def test_published_post_never_carries_source_links(db):
    """Стайлгайд §6: наш пост самодостаточен. Единственные ссылки в нём —
    свои: канал и UGC-бот."""
    from tgtraff.publisher.channels import (CityChannels, TelegramChannel, VkGroup)
    from tgtraff.publisher.format import PostContent, render_post
    from tgtraff.publisher.settings import parse_slots

    city = CityChannels(city=CITY, deep_link="che",
                        tg=TelegramChannel(chat_id="-1001", username="example_channel_1",
                                           title="ЧП Череповец"),
                        vk=VkGroup(), slots=parse_slots(["18:00-22:00"]),
                        min_gap_minutes=20, max_gap_minutes=40, daily_limit=18)
    row = draft(db)
    post_text = render_post(PostContent(city=CITY, title=row["title"], body=row["body"],
                                        category="fire"), city, "example_channel_3").text

    assert "example_channel_7" not in post_text
    assert "t.me/example_channel_7" not in post_text
