"""БД: схема из спеки + гарантии «рестарт не теряет и не дублирует»."""
import sqlite3

import pytest

from tgtraff.config import SourceSpec
from tgtraff.db import Database

SPEC_TABLES = {"sources", "raw_posts", "clusters", "drafts", "published", "subs_metrics", "invite_links"}


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def _src(key="example_channel_7", city="cherepovets", source_type="tg", **kw):
    return SourceSpec(city=city, source_type=source_type, key=key, **kw)


def _post(source_id, external_id="100", **kw):
    return dict(
        source_id=source_id,
        source_type="tg",
        external_id=external_id,
        text="в череповце горел склад",
        url="https://t.me/example_channel_7/100",
        published_at=kw.pop("published_at", "2026-08-28T10:00:00+00:00"),
        **kw,
    )


def test_creates_all_tables_from_spec(db):
    names = {r["name"] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert SPEC_TABLES <= names


def test_init_is_idempotent(tmp_path):
    p = tmp_path / "t.db"
    Database(p).close()
    d = Database(p)          # второй запуск на существующей БД не падает
    assert d.schema_version() >= 1
    d.close()


def test_foreign_keys_and_wal_enabled(db):
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_upsert_source_is_stable_by_key(db):
    first = db.upsert_source(_src(note="лидер"))
    again = db.upsert_source(_src(note="лидер, 37к"))
    assert first == again
    row = db.conn.execute("SELECT note FROM sources WHERE id=?", (first,)).fetchone()
    assert row["note"] == "лидер, 37к"


def test_same_username_in_two_cities_are_separate_sources(db):
    a = db.upsert_source(_src(key="news_74ru", city="magnitogorsk", source_type="tg_regional"))
    b = db.upsert_source(_src(key="news_74ru", city="novokuznetsk", source_type="tg_regional"))
    assert a != b


def test_insert_raw_post_returns_new_then_duplicate(db):
    sid = db.upsert_source(_src())
    first = db.insert_raw_post(**_post(sid))
    dup = db.insert_raw_post(**_post(sid))
    assert first.is_new is True
    assert dup.is_new is False
    assert dup.id == first.id
    assert db.conn.execute("SELECT COUNT(*) c FROM raw_posts").fetchone()["c"] == 1


def test_last_seen_external_id_tracks_max_message_id(db):
    sid = db.upsert_source(_src())
    assert db.last_seen_external_id(sid) is None
    db.insert_raw_post(**_post(sid, external_id="7"))
    db.insert_raw_post(**_post(sid, external_id="42"))
    db.insert_raw_post(**_post(sid, external_id="13"))
    assert db.last_seen_external_id(sid) == 42          # не «последний записанный», а максимальный


def test_last_seen_is_per_source(db):
    a = db.upsert_source(_src(key="a"))
    b = db.upsert_source(_src(key="b"))
    db.insert_raw_post(**_post(a, external_id="500"))
    assert db.last_seen_external_id(b) is None


def test_media_lifecycle_pending_then_done(db):
    sid = db.upsert_source(_src())
    post = db.insert_raw_post(**_post(sid, has_media=1, media_state="pending"))
    mid = db.add_media(post.id, ord=0, kind="photo")
    assert [m.media_id for m in db.pending_media()] == [mid]

    db.mark_media_downloaded(mid, path="/x/1.jpg", size=1234, sha256="ab", mime="image/jpeg")
    assert db.pending_media() == []
    assert db.conn.execute("SELECT media_state FROM raw_posts WHERE id=?", (post.id,)).fetchone()[0] == "done"


def test_failed_media_is_retried_until_attempt_limit(db):
    sid = db.upsert_source(_src())
    post = db.insert_raw_post(**_post(sid, has_media=1, media_state="pending"))
    mid = db.add_media(post.id, ord=0, kind="photo")

    db.mark_media_failed(mid, "timeout")
    assert [m.media_id for m in db.pending_media(max_attempts=3)] == [mid]   # вернётся на ретрай

    db.mark_media_failed(mid, "timeout")
    db.mark_media_failed(mid, "timeout")
    assert db.pending_media(max_attempts=3) == []                           # лимит исчерпан
    assert db.conn.execute("SELECT media_state FROM raw_posts WHERE id=?", (post.id,)).fetchone()[0] == "failed"


def test_pending_media_survives_reopen(tmp_path):
    """Процесс упал между записью поста и скачиванием медиа — после рестарта медиа в очереди."""
    p = tmp_path / "t.db"
    d1 = Database(p)
    sid = d1.upsert_source(_src())
    post = d1.insert_raw_post(**_post(sid, has_media=1, media_state="pending"))
    d1.add_media(post.id, ord=0, kind="photo")
    d1.close()

    d2 = Database(p)
    assert [m.raw_post_id for m in d2.pending_media()] == [post.id]
    d2.close()


def test_raw_post_requires_existing_source(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_raw_post(**_post(999999))


def test_pending_media_returns_freshest_posts_first(db):
    """Скорость публикации — конкурентное преимущество: свежее медиа не должно
    стоять в очереди за историческим догоном."""
    sid = db.upsert_source(_src())
    old = db.insert_raw_post(**_post(sid, external_id="1", published_at="2026-08-01T10:00:00+00:00",
                                     has_media=1, media_state="pending"))
    new = db.insert_raw_post(**_post(sid, external_id="2", published_at="2026-08-28T10:00:00+00:00",
                                     has_media=1, media_state="pending"))
    db.add_media(old.id, ord=0, kind="photo")     # старое встало в очередь первым
    db.add_media(new.id, ord=0, kind="photo")

    assert [m.raw_post_id for m in db.pending_media()] == [new.id, old.id]


def test_stale_media_is_skipped_not_downloaded(db):
    """Догон истории не должен тянуть тысячи старых картинок: свежий аккаунт
    с таким всплеском трафика — кандидат на бан, а старое медиа конвейеру не нужно."""
    sid = db.upsert_source(_src())
    old = db.insert_raw_post(**_post(sid, external_id="1", published_at="2026-07-01T10:00:00+00:00",
                                     has_media=1, media_state="pending"))
    fresh = db.insert_raw_post(**_post(sid, external_id="2", published_at="2026-08-28T10:00:00+00:00",
                                       has_media=1, media_state="pending"))
    db.add_media(old.id, ord=0, kind="photo")
    db.add_media(fresh.id, ord=0, kind="photo")

    assert db.skip_stale_media("2026-08-27T00:00:00+00:00") == 1
    assert [m.raw_post_id for m in db.pending_media()] == [fresh.id]
    assert db.conn.execute("SELECT status FROM raw_media WHERE raw_post_id=?", (old.id,)).fetchone()[0] == "skipped"


def test_skip_stale_media_leaves_downloaded_alone(db):
    sid = db.upsert_source(_src())
    post = db.insert_raw_post(**_post(sid, external_id="1", published_at="2026-07-01T10:00:00+00:00",
                                      has_media=1, media_state="pending"))
    mid = db.add_media(post.id, ord=0, kind="photo")
    db.mark_media_downloaded(mid, path="/x/1.jpg", size=10, sha256="ab", mime="image/jpeg")

    assert db.skip_stale_media("2026-08-27T00:00:00+00:00") == 0
    assert db.conn.execute("SELECT status FROM raw_media WHERE id=?", (mid,)).fetchone()[0] == "done"


def test_post_records_how_it_was_captured(db):
    """Критерий приёмки меряет задержку событийной доставки — её надо отличать
    от догона, у которого лаг измеряется сутками."""
    sid = db.upsert_source(_src())
    live = db.insert_raw_post(**_post(sid, external_id="1", via="live"))
    old = db.insert_raw_post(**_post(sid, external_id="2", via="catchup"))
    got = dict(db.conn.execute(
        "SELECT id, via FROM raw_posts ORDER BY id").fetchall())
    assert got[live.id] == "live" and got[old.id] == "catchup"


def test_via_defaults_to_live(db):
    sid = db.upsert_source(_src())
    res = db.insert_raw_post(**_post(sid))
    assert db.conn.execute("SELECT via FROM raw_posts WHERE id=?", (res.id,)).fetchone()[0] == "live"


def test_missing_column_is_added_to_existing_db(tmp_path):
    """Схема на живой базе доезжает миграцией, а не пересозданием."""
    import sqlite3
    p = tmp_path / "old.db"
    Database(p).close()
    conn = sqlite3.connect(p)
    conn.execute("ALTER TABLE raw_posts DROP COLUMN via")     # откатываем к «старой» схеме
    conn.commit(); conn.close()

    d = Database(p)                                            # переоткрытие чинит схему
    cols = {r[1] for r in d.conn.execute("PRAGMA table_info(raw_posts)")}
    assert "via" in cols
    d.close()


# ---- фаза 2: медиа не только из Telegram ----

def test_media_can_carry_a_download_url(db):
    """У ВК и RSS медиа скачивается по прямой ссылке, а не через Telethon —
    ссылку надо где-то держать между «увидели пост» и «дошли руки скачать»."""
    sid = db.upsert_source(_src(key="vkmgn", source_type="vk"))
    post = db.insert_raw_post(**dict(_post(sid, "-1_2"), source_type="vk"))
    db.add_media(post.id, ord=0, kind="photo", source_url="https://sun9.userapi.com/x.jpg")
    [item] = db.pending_media()
    assert item.source_url == "https://sun9.userapi.com/x.jpg"
    assert item.source_type == "vk"


def test_pending_media_can_be_limited_to_source_types(db):
    """Очередь медиа общая, а качают её разные воркеры: Telethon умеет только ТГ,
    ВК/RSS качаются обычным HTTP. Чужое медиа воркер трогать не должен."""
    tg = db.upsert_source(_src())
    vk = db.upsert_source(_src(key="vkmgn", source_type="vk"))
    tg_post = db.insert_raw_post(**_post(tg, "100"))
    vk_post = db.insert_raw_post(**dict(_post(vk, "-1_2"), source_type="vk"))
    db.add_media(tg_post.id, ord=0, kind="photo")
    db.add_media(vk_post.id, ord=0, kind="photo", source_url="https://sun9.userapi.com/x.jpg")

    assert [m.raw_post_id for m in db.pending_media(source_types=("tg", "tg_regional"))] == [tg_post.id]
    assert [m.raw_post_id for m in db.pending_media(source_types=("vk",))] == [vk_post.id]
    assert len(db.pending_media()) == 2


def test_source_remembers_http_cache_headers(db):
    """RSS опрашивается раз в 5-10 минут: без ETag/Last-Modified мы каждый раз
    тянем ленту целиком и мозолим глаза серверу СМИ."""
    sid = db.upsert_source(_src(key="https://cherinfo.ru", source_type="rss"))
    db.mark_source_polled(sid, etag='W/"abc"', last_modified="Fri, 28 Aug 2026 10:00:00 GMT")
    row = db.get_source(sid)
    assert row["http_etag"] == 'W/"abc"'
    assert row["http_last_modified"] == "Fri, 28 Aug 2026 10:00:00 GMT"
    assert row["last_polled_at"] and row["last_error"] is None


def test_source_error_is_remembered_and_cleared(db):
    """«Источник молчит / отдаёт 500» — это состояние источника, а не повод падать."""
    sid = db.upsert_source(_src(key="https://kuzpress.ru", source_type="rss"))
    db.mark_source_error(sid, "HTTP 503")
    assert db.get_source(sid)["last_error"] == "HTTP 503"
    db.mark_source_polled(sid)
    assert db.get_source(sid)["last_error"] is None


# ---- фаза 2 (R3): служебные отметки конвейера ----

def test_meta_stores_and_returns_a_value(db):
    """Отметки вроде «когда заведена сессия» нужны и прогреву (инвайты не раньше
    чем через сутки), и оператору (когда можно сбрасывать чужие авторизации)."""
    db.set_meta("session_created_at", "2026-09-02T19:00:00+00:00")
    assert db.get_meta("session_created_at") == "2026-09-02T19:00:00+00:00"


def test_meta_returns_default_when_missing(db):
    assert db.get_meta("нет-такого") is None
    assert db.get_meta("нет-такого", "по умолчанию") == "по умолчанию"


def test_meta_value_is_overwritten_not_duplicated(db):
    db.set_meta("k", "первое")
    db.set_meta("k", "второе")
    assert db.get_meta("k") == "второе"
    assert db.conn.execute("SELECT COUNT(*) c FROM meta WHERE key='k'").fetchone()["c"] == 1


def test_meta_survives_reopen(tmp_path):
    d1 = Database(tmp_path / "m.db")
    d1.set_meta("session_created_at", "2026-09-02T19:00:00+00:00")
    d1.close()
    d2 = Database(tmp_path / "m.db")
    assert d2.get_meta("session_created_at") == "2026-09-02T19:00:00+00:00"
    d2.close()


# ---- §17: ретеншн медиа отфильтрованных постов ----

def test_media_of_filtered_posts_is_purged(db, tmp_path):
    """Пост, не прошедший фильтр города, драфтом не станет — его картинка на диске
    только занимает место (решение ревью §17)."""
    sid = db.upsert_source(_src(source_type="tg_regional", key="news_74ru"))
    kept = db.insert_raw_post(**_post(sid, "1"))
    dropped = db.insert_raw_post(**dict(_post(sid, "2"), filtered_out=1))

    keep_file, drop_file = tmp_path / "keep.jpg", tmp_path / "drop.jpg"
    keep_file.write_bytes(b"keep")
    drop_file.write_bytes(b"drop")
    m1 = db.add_media(kept.id, ord=0, kind="photo")
    m2 = db.add_media(dropped.id, ord=0, kind="photo")
    db.mark_media_downloaded(m1, path=str(keep_file), size=4, sha256="a", mime="image/jpeg")
    db.mark_media_downloaded(m2, path=str(drop_file), size=4, sha256="b", mime="image/jpeg")

    assert db.purge_filtered_media() == 1

    assert keep_file.exists() and not drop_file.exists()
    assert db.conn.execute("SELECT status FROM raw_media WHERE id=?", (m2,)).fetchone()["status"] == "skipped"
    assert db.conn.execute("SELECT status FROM raw_media WHERE id=?", (m1,)).fetchone()["status"] == "done"


def test_purge_is_idempotent(db, tmp_path):
    sid = db.upsert_source(_src(source_type="tg_regional", key="news_74ru"))
    post = db.insert_raw_post(**dict(_post(sid, "2"), filtered_out=1))
    f = tmp_path / "x.jpg"
    f.write_bytes(b"x")
    mid = db.add_media(post.id, ord=0, kind="photo")
    db.mark_media_downloaded(mid, path=str(f), size=1, sha256="c", mime=None)
    assert db.purge_filtered_media() == 1
    assert db.purge_filtered_media() == 0


def test_purge_survives_a_file_that_is_already_gone(db, tmp_path):
    """Файл могли удалить руками — это не повод падать."""
    sid = db.upsert_source(_src(source_type="tg_regional", key="news_74ru"))
    post = db.insert_raw_post(**dict(_post(sid, "3"), filtered_out=1))
    mid = db.add_media(post.id, ord=0, kind="photo")
    db.mark_media_downloaded(mid, path=str(tmp_path / "нет-файла.jpg"), size=1, sha256="d", mime=None)
    assert db.purge_filtered_media() == 1
