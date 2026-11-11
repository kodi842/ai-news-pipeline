"""Скачивание медиа ВК и RSS обычным HTTP.

CLAUDE.md: «Медиа перезаливается файлом» — значит любое медиа кластера должно
лежать у нас на диске, а не ссылкой на чужой сервер. У Telegram это делает Telethon,
у остальных источников — этот воркер.
"""
import hashlib

import pytest

from tgtraff.collector.http_media import HttpMediaDownloader, safe_name
from tgtraff.config import SourceSpec, load_config
from tgtraff.db import Database

VK = SourceSpec(city="magnitogorsk", source_type="vk", key="vkmgn")
RSS = SourceSpec(city="cherepovets", source_type="rss", key="https://cherinfo.ru/rss")


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "m.db")
    yield d
    d.close()


@pytest.fixture
def cfg():
    return load_config()


class FakeFetcher:
    """url -> (status, bytes, content_type) либо исключение."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def __call__(self, url, timeout=None):
        self.calls.append(url)
        r = self.responses.get(url, (404, b"", ""))
        if isinstance(r, Exception):
            raise r
        return r


def queue_media(db, spec, external_id="-1_2", source_url="https://sun9.userapi.com/a.jpg",
                kind="photo"):
    source_id = db.upsert_source(spec)
    post = db.insert_raw_post(source_id=source_id, source_type=spec.source_type,
                              external_id=external_id, published_at="2026-08-28T10:00:00+00:00",
                              text="ДТП", has_media=1, media_state="pending")
    db.add_media(post.id, ord=0, kind=kind, source_url=source_url)
    return post.id


def media_row(db, post_id):
    return db.conn.execute("SELECT * FROM raw_media WHERE raw_post_id=?", (post_id,)).fetchone()


async def test_downloads_file_and_records_hash_and_size(db, cfg, tmp_path):
    body = b"\xff\xd8picture-bytes"
    d = HttpMediaDownloader(FakeFetcher({"https://sun9.userapi.com/a.jpg": (200, body, "image/jpeg")}),
                            db, cfg, media_dir=tmp_path / "media")
    post_id = queue_media(db, VK)

    assert await d.download_pending() == 1

    row = media_row(db, post_id)
    assert row["status"] == "done"
    assert row["file_size"] == len(body)
    assert row["sha256"] == hashlib.sha256(body).hexdigest()
    assert open(row["file_path"], "rb").read() == body


async def test_file_lands_in_per_city_per_source_folder(db, cfg, tmp_path):
    d = HttpMediaDownloader(FakeFetcher({"https://sun9.userapi.com/a.jpg": (200, b"x", "image/jpeg")}),
                            db, cfg, media_dir=tmp_path / "media")
    post_id = queue_media(db, VK)
    await d.download_pending()
    path = media_row(db, post_id)["file_path"]
    assert "/magnitogorsk/" in path and "/vkmgn/" in path


async def test_rss_source_url_is_turned_into_a_safe_folder_name(db, cfg, tmp_path):
    """Ключ RSS-источника — это URL: в имя каталога он не годится как есть."""
    d = HttpMediaDownloader(FakeFetcher({"https://cherinfo.ru/img/1.jpg": (200, b"x", "image/jpeg")}),
                            db, cfg, media_dir=tmp_path / "media")
    post_id = queue_media(db, RSS, external_id="https://cherinfo.ru/news/1",
                          source_url="https://cherinfo.ru/img/1.jpg")
    await d.download_pending()
    path = media_row(db, post_id)["file_path"]
    assert "://" not in path and media_row(db, post_id)["status"] == "done"


def test_safe_name_keeps_readable_part_and_stays_unique():
    a = safe_name("https://cherinfo.ru/news/1")
    b = safe_name("https://cherinfo.ru/news/2")
    assert a != b
    assert "/" not in a and ":" not in a
    assert len(a) < 80


async def test_media_without_url_is_skipped_with_a_reason(db, cfg, tmp_path):
    """Видео ВК сервисным ключом не забрать — очередь не должна крутить его вечно."""
    d = HttpMediaDownloader(FakeFetcher({}), db, cfg, media_dir=tmp_path / "media")
    post_id = queue_media(db, VK, source_url=None, kind="video")

    assert await d.download_pending() == 0

    row = media_row(db, post_id)
    assert row["status"] == "skipped" and row["error"]
    assert db.pending_media(source_types=("vk",)) == []


async def test_http_error_is_retried_then_given_up(db, cfg, tmp_path):
    d = HttpMediaDownloader(FakeFetcher({"https://sun9.userapi.com/a.jpg": (500, b"", "")}),
                            db, cfg, media_dir=tmp_path / "media")
    post_id = queue_media(db, VK)
    for _ in range(cfg.collector.media_max_attempts):
        await d.download_pending()
    row = media_row(db, post_id)
    assert row["status"] == "failed" and row["attempts"] == cfg.collector.media_max_attempts
    assert db.pending_media(source_types=("vk",)) == []      # попытки исчерпаны


async def test_network_error_does_not_stop_the_queue(db, cfg, tmp_path):
    d = HttpMediaDownloader(
        FakeFetcher({"https://a/1.jpg": OSError("reset"), "https://b/2.jpg": (200, b"ok", "image/jpeg")}),
        db, cfg, media_dir=tmp_path / "media")
    queue_media(db, VK, external_id="-1_1", source_url="https://a/1.jpg")
    good = queue_media(db, VK, external_id="-1_2", source_url="https://b/2.jpg")

    assert await d.download_pending() == 1
    assert media_row(db, good)["status"] == "done"


async def test_oversized_file_is_skipped(db, cfg, tmp_path):
    big = b"x" * (cfg.collector.media_max_bytes + 1)
    d = HttpMediaDownloader(FakeFetcher({"https://sun9.userapi.com/a.jpg": (200, big, "image/jpeg")}),
                            db, cfg, media_dir=tmp_path / "media")
    post_id = queue_media(db, VK)
    await d.download_pending()
    assert media_row(db, post_id)["status"] == "skipped"


async def test_telegram_media_is_left_to_telethon(db, cfg, tmp_path):
    d = HttpMediaDownloader(FakeFetcher({}), db, cfg, media_dir=tmp_path / "media")
    tg = SourceSpec(city="cherepovets", source_type="tg", key="example_channel_7")
    post_id = queue_media(db, tg, external_id="512", source_url=None)

    assert await d.download_pending() == 0
    assert media_row(db, post_id)["status"] == "pending"     # ждёт своего воркера


async def test_extension_comes_from_content_type_when_url_has_none(db, cfg, tmp_path):
    d = HttpMediaDownloader(FakeFetcher({"https://sun9.userapi.com/img?id=7": (200, b"x", "image/png")}),
                            db, cfg, media_dir=tmp_path / "media")
    post_id = queue_media(db, VK, source_url="https://sun9.userapi.com/img?id=7")
    await d.download_pending()
    assert media_row(db, post_id)["file_path"].endswith(".png")
