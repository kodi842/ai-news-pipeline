"""Нормализатор: единый формат поста и склейка альбомов (спека §2, QUESTIONS §4).

Дальше по конвейеру (dedup → rewriter) идёт не строка raw_posts, а логический пост:
альбом из N сообщений Telegram — это ОДНА новость с N медиа, а не N новостей.
"""
import pytest

from tgtraff.normalize import glue_albums

BASE = {
    "source_id": 10, "source_type": "tg", "city": "cherepovets", "source_key": "example_channel_7",
    "text": "", "published_at": "2026-08-28T10:00:00+00:00", "url": None,
    "views": None, "grouped_id": None, "media": [],
}


def row(raw_post_id, external_id, **kw):
    r = dict(BASE, raw_post_id=raw_post_id, external_id=str(external_id))
    r["url"] = f"https://t.me/example_channel_7/{external_id}"
    r.update(kw)
    return r


def media(ord_, kind="photo", status="done", path=None):
    return {"ord": ord_, "kind": kind, "status": status,
            "file_path": path or f"/media/{ord_}.jpg"}


# ---- одиночные посты ----

def test_single_post_becomes_single_item():
    [item] = glue_albums([row(1, 512, text="Горел склад", views=340, media=[media(0)])])
    assert item.external_id == "512"
    assert item.text == "Горел склад"
    assert item.views == 340
    assert item.source_type == "tg" and item.city == "cherepovets"
    assert [m.kind for m in item.media] == ["photo"]
    assert item.raw_post_ids == [1]


def test_post_without_media_has_empty_media_list():
    [item] = glue_albums([row(1, 512, text="просто текст")])
    assert item.media == [] and item.has_media is False


def test_posts_keep_publication_order_newest_first():
    items = glue_albums([
        row(1, 10, published_at="2026-08-28T10:00:00+00:00"),
        row(2, 11, published_at="2026-08-28T12:00:00+00:00"),
    ])
    assert [i.external_id for i in items] == ["11", "10"]


# ---- альбомы (grouped_id) ----

def test_album_of_three_messages_is_one_post_with_three_media():
    """Ключевой критерий приёмки фазы 2: альбом из N медиа — один нормализованный пост."""
    rows = [
        row(1, 100, grouped_id="777", text="", media=[media(0)]),
        row(2, 101, grouped_id="777", text="Пожар на Победе, 3 машины", media=[media(0)]),
        row(3, 102, grouped_id="777", text="", media=[media(0, kind="video")]),
    ]
    [item] = glue_albums(rows)
    assert item.text == "Пожар на Победе, 3 машины"     # подпись альбома — на одном из сообщений
    assert [m.kind for m in item.media] == ["photo", "photo", "video"]
    assert [m.ord for m in item.media] == [0, 1, 2]     # сквозная нумерация внутри поста
    assert item.raw_post_ids == [1, 2, 3]
    assert item.external_id == "100"                    # id первого сообщения альбома
    assert item.is_album is True


def test_album_takes_earliest_publication_time_and_first_url():
    rows = [
        row(2, 101, grouped_id="777", published_at="2026-08-28T10:00:05+00:00"),
        row(1, 100, grouped_id="777", published_at="2026-08-28T10:00:00+00:00"),
    ]
    [item] = glue_albums(rows)
    assert item.published_at == "2026-08-28T10:00:00+00:00"
    assert item.url == "https://t.me/example_channel_7/100"


def test_album_views_are_taken_from_the_richest_message():
    """У сообщений альбома счётчик просмотров разъезжается — берём максимум."""
    rows = [row(1, 100, grouped_id="777", views=1200),
            row(2, 101, grouped_id="777", views=None),
            row(3, 102, grouped_id="777", views=1190)]
    assert glue_albums(rows)[0].views == 1200


def test_two_albums_from_one_source_do_not_mix():
    rows = [row(1, 100, grouped_id="777", media=[media(0)]),
            row(2, 101, grouped_id="777", media=[media(0)]),
            row(3, 200, grouped_id="888", media=[media(0)])]
    items = glue_albums(rows)
    assert len(items) == 2
    assert sorted(len(i.media) for i in items) == [1, 2]


def test_same_grouped_id_in_two_sources_stays_separate():
    """grouped_id уникален в пределах чата, а не глобально — склеивать разные
    источники по нему нельзя, иначе новости двух городов слипнутся в одну."""
    rows = [row(1, 100, grouped_id="777"),
            row(2, 100, grouped_id="777", source_id=20, source_key="dtp_che35")]
    assert len(glue_albums(rows)) == 2


def test_album_and_single_post_coexist():
    rows = [row(1, 100, grouped_id="777"), row(2, 101, grouped_id="777"), row(3, 300, text="одиночный")]
    items = glue_albums(rows)
    assert len(items) == 2
    assert {i.is_album for i in items} == {True, False}


def test_album_without_any_caption_keeps_empty_text():
    rows = [row(1, 100, grouped_id="777", media=[media(0)]),
            row(2, 101, grouped_id="777", media=[media(0)])]
    assert glue_albums(rows)[0].text == ""


def test_longest_caption_wins_when_several_messages_carry_text():
    """Изредка подпись дублируется обрезанной — берём самую полную фактуру."""
    rows = [row(1, 100, grouped_id="777", text="Пожар"),
            row(2, 101, grouped_id="777", text="Пожар на Победе, горят три машины")]
    assert glue_albums(rows)[0].text == "Пожар на Победе, горят три машины"


# ---- источники, отличные от Telegram: формат один и тот же ----

def test_vk_post_with_several_attachments_is_one_item():
    """У ВК альбом приходит одним постом — склейка не нужна, формат тот же."""
    vk = row(1, "-12345_678", source_type="vk", source_key="vkmgn", city="magnitogorsk",
             text="ДТП на Ленина", media=[media(0), media(1), media(2, kind="video")],
             url="https://vk.com/wall-12345_678")
    [item] = glue_albums([vk])
    assert item.source_type == "vk"
    assert len(item.media) == 3 and item.is_album is False
    assert item.url == "https://vk.com/wall-12345_678"


def test_rss_entry_is_one_item_without_media():
    rss = row(1, "https://cherinfo.ru/news/1", source_type="rss", source_key="https://cherinfo.ru",
              text="Мэрия закрыла мост", url="https://cherinfo.ru/news/1")
    [item] = glue_albums([rss])
    assert item.source_type == "rss" and item.media == []


def test_mixed_sources_normalise_to_the_same_shape():
    items = glue_albums([
        row(1, 100, text="тг"),
        row(2, "-1_2", source_type="vk", source_key="vkmgn", text="вк"),
        row(3, "guid", source_type="rss", source_key="https://cherinfo.ru", text="рсс"),
    ])
    fields = {"source_id", "source_type", "city", "source_key", "external_id",
              "text", "published_at", "url", "views", "media"}
    for item in items:
        assert fields <= set(vars(item))


# ---- медиа внутри поста ----

def test_media_of_album_keeps_message_order_not_insertion_order():
    rows = [row(2, 102, grouped_id="777", media=[media(0, path="/b.jpg")]),
            row(1, 100, grouped_id="777", media=[media(0, path="/a.jpg")])]
    assert [m.file_path for m in glue_albums(rows)[0].media] == ["/a.jpg", "/b.jpg"]


def test_undownloaded_media_is_reported_as_not_ready():
    rows = [row(1, 100, media=[media(0, status="done"), media(1, status="pending")])]
    item = glue_albums(rows)[0]
    assert item.media_ready is False
    assert glue_albums([row(2, 101, media=[media(0, status="done")])])[0].media_ready is True


def test_item_without_media_is_ready():
    assert glue_albums([row(1, 100, text="текст")])[0].media_ready is True


@pytest.mark.parametrize("bad_status", ["failed", "skipped"])
def test_failed_media_does_not_block_readiness_forever(bad_status):
    """Скачивание могло не удаться — пост всё равно едет дальше (медиа-каскад
    решит, что ставить), но помечаем, что файлов меньше, чем медиа в источнике."""
    item = glue_albums([row(1, 100, media=[media(0, status=bad_status)])])[0]
    assert item.media_ready is True
    assert item.media_missing == 1


# ---- чтение ленты из БД в едином формате ----

from tgtraff.config import SourceSpec          # noqa: E402
from tgtraff.db import Database                # noqa: E402
from tgtraff.normalize import normalized_feed  # noqa: E402


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "n.db")
    yield d
    d.close()


def add_source(db, city="cherepovets", source_type="tg", key="example_channel_7"):
    return db.upsert_source(SourceSpec(city=city, source_type=source_type, key=key))


def add_post(db, source_id, external_id, source_type="tg", published_at="2026-08-28T10:00:00+00:00",
             media_kinds=(), **kw):
    res = db.insert_raw_post(source_id=source_id, source_type=source_type,
                             external_id=str(external_id), published_at=published_at, **kw)
    for i, kind in enumerate(media_kinds):
        db.add_media(res.id, ord=i, kind=kind)
    return res.id


def test_feed_returns_normalized_items(db):
    sid = add_source(db)
    add_post(db, sid, 512, text="Горел склад", views=340, has_media=1, media_kinds=("photo",))
    [item] = normalized_feed(db)
    assert (item.source_type, item.city, item.source_key) == ("tg", "cherepovets", "example_channel_7")
    assert item.text == "Горел склад" and item.views == 340
    assert [m.kind for m in item.media] == ["photo"]


def test_feed_glues_album_from_db(db):
    """Сквозная проверка критерия приёмки: три сообщения альбома в БД — один пост в ленте."""
    sid = add_source(db)
    for mid, text in ((100, ""), (101, "Пожар на Победе"), (102, "")):
        add_post(db, sid, mid, text=text, grouped_id="777", has_media=1, media_kinds=("photo",))
    [item] = normalized_feed(db)
    assert item.is_album and len(item.media) == 3
    assert item.text == "Пожар на Победе"


def test_feed_skips_posts_filtered_by_city(db):
    """Областной пост без упоминания города дальше нормализатора не идёт (спека, фаза 1)."""
    sid = add_source(db, source_type="tg_regional", key="news_74ru", city="magnitogorsk")
    add_post(db, sid, 1, source_type="tg_regional", text="В Челябинске развязка",
             filtered_out=1, filter_reason="city_filter:магнитогорск")
    add_post(db, sid, 2, source_type="tg_regional", text="В Магнитогорске ДТП")
    assert [i.external_id for i in normalized_feed(db)] == ["2"]


def test_feed_can_be_limited_to_one_city(db):
    che = add_source(db)
    mgn = add_source(db, city="magnitogorsk", key="example_channel_13")
    add_post(db, che, 1, text="че")
    add_post(db, mgn, 2, text="мгн")
    assert [i.city for i in normalized_feed(db, city="magnitogorsk")] == ["magnitogorsk"]


def test_feed_window_cuts_off_old_posts(db):
    sid = add_source(db)
    add_post(db, sid, 1, text="старое", published_at="2026-08-20T10:00:00+00:00")
    add_post(db, sid, 2, text="свежее", published_at="2026-08-28T10:00:00+00:00")
    assert [i.text for i in normalized_feed(db, since="2026-08-27T00:00:00+00:00")] == ["свежее"]


def test_feed_mixes_all_source_types_in_one_shape(db):
    """Критерий приёмки фазы 2: ТГ, ВК и RSS лежат в одной таблице в едином формате."""
    tg = add_source(db)
    vk = add_source(db, source_type="vk", key="cityche")
    rss = add_source(db, source_type="rss", key="https://cherinfo.ru")
    add_post(db, tg, 100, text="тг")
    add_post(db, vk, "-1_2", source_type="vk", text="вк", media_kinds=("photo", "video"))
    add_post(db, rss, "https://cherinfo.ru/news/1", source_type="rss", text="рсс")

    feed = normalized_feed(db)
    assert {i.source_type for i in feed} == {"tg", "vk", "rss"}
    assert all(isinstance(i.text, str) and i.published_at for i in feed)
    assert len([i for i in feed if i.has_media]) == 1
