"""RSS-коллектор: ленты городских СМИ и ведомств (спека, §1 Collector).

Сеть не трогаем: фетчер внедряется, и тесты отдают заранее записанные ответы —
включая 500, таймаут и мусор вместо ленты (критерий приёмки «поллинг переживает
недоступность источника»).
"""
import pytest

from tgtraff.collector.rss_collector import RssCollector, parse_feed
from tgtraff.collector.rss_ingest import normalize_rss_entry, strip_html
from tgtraff.config import SourceSpec, load_config
from tgtraff.db import Database

CHE = SourceSpec(city="cherepovets", source_type="rss", key="https://cherinfo.ru/rss")
REGIONAL = SourceSpec(city="magnitogorsk", source_type="rss", key="https://74.ru/rss",
                      city_filter="магнитогорск")

FEED = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
  <title>cherinfo.ru</title>
  <item>
    <title>На Октябрьском мосту столкнулись три машины</title>
    <link>https://cherinfo.ru/news/1</link>
    <guid>https://cherinfo.ru/news/1</guid>
    <description>&lt;p&gt;ДТП произошло утром.&lt;/p&gt;</description>
    <pubDate>Fri, 28 Aug 2026 07:30:00 +0300</pubDate>
    <enclosure url="https://cherinfo.ru/img/1.jpg" type="image/jpeg" length="12345"/>
  </item>
  <item>
    <title>Мэрия отчиталась о ремонте</title>
    <link>https://cherinfo.ru/news/2</link>
    <guid>https://cherinfo.ru/news/2</guid>
    <description>Текст без медиа</description>
    <pubDate>Fri, 28 Aug 2026 06:00:00 +0300</pubDate>
  </item>
</channel></rss>
"""


# ---- разбор ленты и нормализация ----

def test_parses_entries_from_feed():
    entries = parse_feed(FEED.encode("utf-8"))
    assert len(entries) == 2
    assert entries[0].get("title") == "На Октябрьском мосту столкнулись три машины"


def test_entry_maps_to_common_shape():
    entry = parse_feed(FEED.encode("utf-8"))[0]
    p = normalize_rss_entry(entry, CHE)
    assert p.external_id == "https://cherinfo.ru/news/1"
    assert p.url == "https://cherinfo.ru/news/1"
    assert p.published_at.startswith("2026-08-28T04:30")     # +03:00 → UTC
    assert "На Октябрьском мосту" in p.text and "ДТП произошло утром." in p.text


def test_enclosure_image_becomes_media_with_url():
    entry = parse_feed(FEED.encode("utf-8"))[0]
    p = normalize_rss_entry(entry, CHE)
    assert [(m.kind, m.url) for m in p.media] == [("photo", "https://cherinfo.ru/img/1.jpg")]


def test_entry_without_media_has_none():
    p = normalize_rss_entry(parse_feed(FEED.encode("utf-8"))[1], CHE)
    assert p.media == [] and p.media_state == "none"


def test_html_is_stripped_from_description():
    """Абзацы остаются абзацами: слипшийся в одну строку текст хуже читается
    и хуже режется на фразы в rewriter'е."""
    assert strip_html("<p>Пожар на <b>Ленина</b>.</p>Пострадавших нет") == \
        "Пожар на Ленина.\nПострадавших нет"


def test_run_of_break_tags_collapses_to_one_blank_line():
    """Ленты СМИ щедры на <br><br><br> — в тексте поста это мусор."""
    assert strip_html("<p>Пожар.</p><br><br><br>Пострадавших нет") == "Пожар.\n\nПострадавших нет"


def test_html_entities_are_decoded():
    assert strip_html("Ремонт &laquo;Северного&raquo; &amp; развязки") == "Ремонт «Северного» & развязки"


def test_script_and_style_do_not_leak_into_text():
    assert strip_html("<style>.a{color:red}</style>Текст<script>alert(1)</script>") == "Текст"


def test_entry_without_date_falls_back_to_fetch_time():
    """Часть лент не отдаёт pubDate. Без времени пост не встанет в окно dedup,
    поэтому берём момент получения — он не позже реального."""
    entry = {"title": "Без даты", "link": "https://x/1", "id": "https://x/1"}
    p = normalize_rss_entry(entry, CHE, fetched_at="2026-08-28T10:00:00+00:00")
    assert p.published_at == "2026-08-28T10:00:00+00:00"


def test_entry_without_guid_uses_link_as_id():
    p = normalize_rss_entry({"title": "т", "link": "https://x/9"}, CHE)
    assert p.external_id == "https://x/9"


def test_city_filter_applies_to_regional_feeds():
    on_topic = {"title": "В Магнитогорске ДТП", "link": "https://74.ru/1", "summary": ""}
    off_topic = {"title": "В Челябинске развязка", "link": "https://74.ru/2", "summary": ""}
    assert normalize_rss_entry(off_topic, REGIONAL).filtered_out is True
    assert normalize_rss_entry(on_topic, REGIONAL).filtered_out is False


# ---- поллинг ----

@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "rss.db")
    yield d
    d.close()


@pytest.fixture
def cfg():
    return load_config()


class FakeFetcher:
    """url -> (status, body, headers) либо исключение."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def __call__(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        r = self.responses.get(url, (404, b"", {}))
        if isinstance(r, list):
            r = r.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def collector(db, cfg, responses, sources=(CHE,)):
    c = RssCollector(FakeFetcher(responses), db, cfg)
    c.register(list(sources))
    return c


async def test_poll_stores_entries(db, cfg):
    c = collector(db, cfg, {CHE.key: (200, FEED.encode("utf-8"), {})})
    assert await c.poll_once() == 2
    rows = db.conn.execute("SELECT * FROM raw_posts ORDER BY published_at").fetchall()
    assert [r["source_type"] for r in rows] == ["rss", "rss"]
    assert rows[0]["url"] == "https://cherinfo.ru/news/2"
    assert rows[0]["via"] == "poll"


async def test_media_of_entry_is_queued_with_url(db, cfg):
    c = collector(db, cfg, {CHE.key: (200, FEED.encode("utf-8"), {})})
    await c.poll_once()
    media = db.conn.execute("SELECT * FROM raw_media").fetchall()
    assert [m["source_url"] for m in media] == ["https://cherinfo.ru/img/1.jpg"]
    assert media[0]["status"] == "pending"


async def test_second_poll_adds_nothing_new(db, cfg):
    c = collector(db, cfg, {CHE.key: (200, FEED.encode("utf-8"), {})})
    assert await c.poll_once() == 2
    assert await c.poll_once() == 0
    assert db.count_raw_posts() == 2


async def test_conditional_get_headers_are_sent_next_time(db, cfg):
    """ETag/Last-Modified бережёт и наш трафик, и терпение сервера СМИ."""
    c = collector(db, cfg, {CHE.key: [
        (200, FEED.encode("utf-8"), {"ETag": 'W/"v1"', "Last-Modified": "Fri, 28 Aug 2026 07:30:00 GMT"}),
        (304, b"", {}),
    ]})
    await c.poll_once()
    assert await c.poll_once() == 0
    _, headers = c.fetch.calls[1]
    assert headers["If-None-Match"] == 'W/"v1"'
    assert headers["If-Modified-Since"] == "Fri, 28 Aug 2026 07:30:00 GMT"


async def test_server_error_is_recorded_and_does_not_raise(db, cfg):
    c = collector(db, cfg, {CHE.key: (503, b"", {})})
    assert await c.poll_once() == 0
    assert "503" in db.conn.execute("SELECT last_error FROM sources").fetchone()["last_error"]


async def test_network_failure_is_recorded_and_does_not_raise(db, cfg):
    c = collector(db, cfg, {CHE.key: OSError("connection reset")})
    assert await c.poll_once() == 0
    assert "connection reset" in db.conn.execute("SELECT last_error FROM sources").fetchone()["last_error"]


async def test_html_page_instead_of_feed_is_reported_as_broken_source(db, cfg):
    c = collector(db, cfg, {CHE.key: (200, b"<html><body>404 not found</body></html>", {})})
    assert await c.poll_once() == 0
    assert db.conn.execute("SELECT last_error FROM sources").fetchone()["last_error"]


async def test_one_broken_feed_does_not_stop_the_others(db, cfg):
    """Критерий приёмки фазы 2: недоступность источника не ломает опрос остальных."""
    other = SourceSpec(city="cherepovets", source_type="rss", key="https://35media.ru/rss")
    c = collector(db, cfg,
                  {CHE.key: OSError("timeout"), other.key: (200, FEED.encode("utf-8"), {})},
                  sources=(CHE, other))
    assert await c.poll_once() == 2
    errs = {r["key"]: r["last_error"] for r in db.conn.execute("SELECT key, last_error FROM sources")}
    assert errs[CHE.key] and errs[other.key] is None


async def test_successful_poll_clears_previous_error(db, cfg):
    c = collector(db, cfg, {CHE.key: [OSError("timeout"), (200, FEED.encode("utf-8"), {})]})
    await c.poll_once()
    await c.poll_once()
    assert db.conn.execute("SELECT last_error FROM sources").fetchone()["last_error"] is None


async def test_entries_beyond_limit_are_not_read(db, cfg):
    many = FEED.replace("</channel>", "".join(
        f"<item><title>n{i}</title><link>https://cherinfo.ru/n/{i}</link>"
        f"<guid>https://cherinfo.ru/n/{i}</guid></item>" for i in range(50)) + "</channel>")
    c = collector(db, cfg, {CHE.key: (200, many.encode("utf-8"), {})})
    stored = await c.poll_once()
    assert stored == cfg.rss.entries_per_feed


# ---- §17: одна содержательная картинка на запись ----

def entry_with(**kw):
    return dict({"title": "Пожар на Ленина", "link": "https://x/1", "summary": ""}, **kw)


def test_only_one_image_is_taken_from_an_entry():
    """Ленты СМИ отдают по 4-6 картинок на запись, а посту нужна одна:
    так медиа-каталог не растёт гигабайтами (решение ревью §17)."""
    e = entry_with(enclosures=[
        {"href": "https://x/1.jpg", "type": "image/jpeg"},
        {"href": "https://x/2.jpg", "type": "image/jpeg"},
        {"href": "https://x/3.jpg", "type": "image/jpeg"},
    ])
    assert [m.url for m in normalize_rss_entry(e, CHE).media] == ["https://x/1.jpg"]


def test_tiny_image_is_skipped_in_favour_of_a_real_one():
    """Первым в ленте часто идёт логотип или иконка — содержательная картинка ниже."""
    e = entry_with(media_content=[
        {"url": "https://x/logo.png", "medium": "image", "width": "64", "height": "64"},
        {"url": "https://x/photo.jpg", "medium": "image", "width": "1200", "height": "800"},
    ])
    assert [m.url for m in normalize_rss_entry(e, CHE).media] == ["https://x/photo.jpg"]


def test_thumbnail_is_used_only_when_nothing_better_exists():
    e = entry_with(media_thumbnail=[{"url": "https://x/thumb.jpg"}],
                   enclosures=[{"href": "https://x/big.jpg", "type": "image/jpeg"}])
    assert [m.url for m in normalize_rss_entry(e, CHE).media] == ["https://x/big.jpg"]

    only_thumb = entry_with(media_thumbnail=[{"url": "https://x/thumb.jpg"}])
    assert [m.url for m in normalize_rss_entry(only_thumb, CHE).media] == ["https://x/thumb.jpg"]


def test_entry_with_no_usable_image_has_no_media():
    e = entry_with(media_content=[{"url": "https://x/icon.png", "medium": "image",
                                   "width": "16", "height": "16"}])
    assert normalize_rss_entry(e, CHE).media == []


async def test_feed_queues_one_media_per_entry(db, cfg):
    """Сквозная проверка: у записи ленты в очереди ровно одна картинка."""
    feed = FEED.replace('<enclosure url="https://cherinfo.ru/img/1.jpg" type="image/jpeg" length="12345"/>',
                        '<enclosure url="https://cherinfo.ru/img/1.jpg" type="image/jpeg" length="1"/>'
                        '<enclosure url="https://cherinfo.ru/img/2.jpg" type="image/jpeg" length="2"/>'
                        '<enclosure url="https://cherinfo.ru/img/3.jpg" type="image/jpeg" length="3"/>')
    c = collector(db, cfg, {CHE.key: (200, feed.encode("utf-8"), {})})
    await c.poll_once()
    queued = [m["source_url"] for m in db.conn.execute("SELECT source_url FROM raw_media")]
    assert queued == ["https://cherinfo.ru/img/1.jpg"]
