"""ВК-коллектор: официальный API, wall.get батчами через execute (спека, §1 Collector).

Сеть в тестах не трогаем: VkApi принимает транспорт, и мы отдаём ему заранее
записанные ответы ВК — включая ответы со сбоями, ради критерия приёмки
«поллинг переживает недоступность источника».
"""
import pytest

from tgtraff.collector.vk_collector import VkApi, VkCollector, VkError
from tgtraff.collector.vk_ingest import normalize_vk_post
from tgtraff.config import SourceSpec, load_config
from tgtraff.db import Database

MGN = SourceSpec(city="magnitogorsk", source_type="vk", key="vkmgn")
REGIONAL = SourceSpec(city="magnitogorsk", source_type="vk", key="chel_obl", city_filter="магнитогорск")


def photo(url="https://sun9.userapi.com/big.jpg"):
    return {"type": "photo", "photo": {"id": 1, "owner_id": -123, "sizes": [
        {"type": "m", "url": "https://sun9.userapi.com/small.jpg", "width": 130, "height": 87},
        {"type": "x", "url": url, "width": 604, "height": 403},
    ]}}


def video():
    return {"type": "video", "video": {"id": 456, "owner_id": -123, "title": "ДТП",
                                       "image": [{"url": "https://sun9.userapi.com/thumb.jpg",
                                                  "width": 320, "height": 240}]}}


def doc(ext="gif", url="https://vk.com/doc-1_2.gif"):
    return {"type": "doc", "doc": {"id": 2, "owner_id": -123, "title": f"clip.{ext}",
                                   "ext": ext, "url": url}}


def wall_item(post_id=678, text="ДТП на Ленина", owner_id=-12345, ts=1787000000, **kw):
    return dict({"id": post_id, "owner_id": owner_id, "from_id": owner_id, "date": ts,
                 "text": text, "attachments": [], "views": {"count": 1200},
                 "likes": {"count": 10}, "marked_as_ads": 0}, **kw)


# ---- нормализация поста ВК в общий формат ----

def test_maps_core_fields_to_common_shape():
    p = normalize_vk_post(wall_item(), MGN)
    assert p.external_id == "-12345_678"          # канонический id поста ВК
    assert p.text == "ДТП на Ленина"
    assert p.url == "https://vk.com/wall-12345_678"
    assert p.views == 1200
    assert p.published_at.endswith("+00:00")
    assert p.filtered_out is False


def test_photo_attachment_becomes_media_with_biggest_size_url():
    p = normalize_vk_post(wall_item(attachments=[photo()]), MGN)
    assert [(m.kind, m.url) for m in p.media] == [("photo", "https://sun9.userapi.com/big.jpg")]
    assert p.has_media is True and p.media_state == "pending"


def test_album_of_attachments_is_one_post_with_several_media():
    """У ВК альбом приходит одним постом — N вложений, один пост, склейка не нужна."""
    p = normalize_vk_post(wall_item(attachments=[photo("a.jpg"), photo("b.jpg"), doc()]), MGN)
    assert [m.ord for m in p.media] == [0, 1, 2]
    assert [m.kind for m in p.media] == ["photo", "photo", "photo"]   # gif-док = картинка


def test_video_is_recorded_but_has_no_download_url():
    """Сервисным ключом файл видео у ВК не забрать — фиксируем факт видео,
    а качать нечего (медиа-каскад фазы 5 возьмёт фото или шаблон)."""
    p = normalize_vk_post(wall_item(attachments=[video()]), MGN)
    assert [(m.kind, m.url) for m in p.media] == [("video", None)]


def test_link_preview_is_not_media():
    p = normalize_vk_post(wall_item(attachments=[{"type": "link", "link": {"url": "http://x"}}]), MGN)
    assert p.media == []


def test_repost_takes_text_and_media_from_original():
    """Репост в паблике: свой текст пустой, вся фактура — в copy_history."""
    original = wall_item(post_id=1, owner_id=-999, text="Горит склад на Кирова",
                         attachments=[photo()])
    p = normalize_vk_post(wall_item(text="", copy_history=[original]), MGN)
    assert p.text == "Горит склад на Кирова"
    assert len(p.media) == 1
    assert p.fwd_from == "-999"


def test_repost_with_own_comment_keeps_both_texts():
    original = wall_item(post_id=1, owner_id=-999, text="Горит склад")
    p = normalize_vk_post(wall_item(text="Наши читатели сообщают:", copy_history=[original]), MGN)
    assert "Наши читатели сообщают:" in p.text and "Горит склад" in p.text


def test_advertising_flag_is_kept_for_the_relevance_filter():
    """Отсев рекламы — фаза 4; наша задача сохранить признак, а не потерять его."""
    p = normalize_vk_post(wall_item(marked_as_ads=1), MGN)
    assert p.extra["marked_as_ads"] is True


def test_city_filter_works_for_regional_vk_sources():
    assert normalize_vk_post(wall_item(text="В Челябинске развязка"), REGIONAL).filtered_out is True
    assert normalize_vk_post(wall_item(text="В Магнитогорске ДТП"), REGIONAL).filtered_out is False


def test_empty_post_is_marked_as_nothing_to_publish():
    assert normalize_vk_post(wall_item(text="", attachments=[]), MGN).is_empty is True
    assert normalize_vk_post(wall_item(text="есть текст"), MGN).is_empty is False


# ---- транспорт и ошибки API ----

class FakeTransport:
    """Отдаёт заготовленные ответы VK API по имени метода."""

    def __init__(self, responses):
        self.responses = responses          # method -> ответ или список ответов по вызовам
        self.calls = []

    async def __call__(self, method, params):
        self.calls.append((method, params))
        r = self.responses[method]
        if isinstance(r, list):
            r = r.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


async def test_api_returns_payload():
    api = VkApi(token="t", transport=FakeTransport({"wall.get": {"response": {"items": []}}}))
    assert await api.call("wall.get", domain="vkmgn") == {"items": []}


async def test_api_raises_on_vk_error():
    api = VkApi(token="t", transport=FakeTransport(
        {"wall.get": {"error": {"error_code": 15, "error_msg": "Access denied"}}}))
    with pytest.raises(VkError) as e:
        await api.call("wall.get", domain="closed_group")
    assert e.value.code == 15


async def test_api_retries_on_rate_limit():
    """Ошибка 6 «слишком много запросов» — не отказ источника, а «подожди»."""
    transport = FakeTransport({"wall.get": [
        {"error": {"error_code": 6, "error_msg": "Too many requests per second"}},
        {"response": {"items": [wall_item()]}},
    ]})
    api = VkApi(token="t", transport=transport, retry_pause=0)
    assert len((await api.call("wall.get", domain="vkmgn"))["items"]) == 1
    assert len(transport.calls) == 2


async def test_token_is_sent_with_every_call_and_never_logged():
    transport = FakeTransport({"wall.get": {"response": {}}})
    api = VkApi(token="секретный", transport=transport)
    await api.call("wall.get", domain="x")
    _, params = transport.calls[0]
    assert params["access_token"] == "секретный"
    assert params["v"]


# ---- батч через execute ----

async def test_wall_get_batch_asks_for_all_domains_in_one_request():
    """Спека: wall.get батчами через execute (до 25 пабликов на запрос)."""
    domains = [f"pub{i}" for i in range(25)]
    transport = FakeTransport({"execute": {"response": [{"items": [wall_item()]} for _ in domains]}})
    api = VkApi(token="t", transport=transport)

    result = await api.wall_get_batch(domains, count=20)

    assert len(transport.calls) == 1
    assert transport.calls[0][0] == "execute"
    assert set(result) == set(domains)
    assert all(isinstance(v, list) for v in result.values())


async def test_batch_is_split_by_execute_limit():
    domains = [f"pub{i}" for i in range(30)]
    transport = FakeTransport({"execute": [
        {"response": [{"items": []} for _ in range(25)]},
        {"response": [{"items": []} for _ in range(5)]},
    ]})
    api = VkApi(token="t", transport=transport)
    result = await api.wall_get_batch(domains)
    assert len(transport.calls) == 2 and len(result) == 30


async def test_unavailable_group_does_not_break_the_batch():
    """VK возвращает false вместо ответа по недоступному паблику — остальные должны дойти."""
    transport = FakeTransport({"execute": {
        "response": [{"items": [wall_item()]}, False],
        "execute_errors": [{"method": "wall.get", "error_code": 15, "error_msg": "Access denied"}],
    }})
    api = VkApi(token="t", transport=transport)
    result = await api.wall_get_batch(["good", "closed"])
    assert isinstance(result["good"], list) and len(result["good"]) == 1
    assert isinstance(result["closed"], VkError)


# ---- коллектор ----

@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "vk.db")
    yield d
    d.close()


@pytest.fixture
def cfg():
    return load_config()


class FakeApi:
    def __init__(self, per_domain):
        self.per_domain = per_domain
        self.batches = []

    async def wall_get_batch(self, domains, count=20):
        self.batches.append(list(domains))
        return {d: self.per_domain.get(d, []) for d in domains}


def test_collector_registers_only_verified_vk_sources(db, cfg):
    c = VkCollector(FakeApi({}), db, cfg)
    specs = c.prepare_sources()
    assert specs, "в конфиге есть verified ВК-паблики"
    assert all(s.status == "verified" and s.key for s in specs)
    assert {s.source_type for s in specs} == {"vk"}


async def test_poll_writes_posts_and_queues_media(db, cfg):
    api = FakeApi({"vkmgn": [wall_item(attachments=[photo()])]})
    c = VkCollector(api, db, cfg)
    c.prepare_sources()
    assert await c.poll_once() >= 1

    row = db.conn.execute("SELECT * FROM raw_posts WHERE external_id='-12345_678'").fetchone()
    assert row["source_type"] == "vk" and row["text"] == "ДТП на Ленина"
    media = db.conn.execute("SELECT * FROM raw_media WHERE raw_post_id=?", (row["id"],)).fetchone()
    assert media["source_url"] == "https://sun9.userapi.com/big.jpg"
    assert media["status"] == "pending"


async def test_second_poll_does_not_duplicate_posts(db, cfg):
    api = FakeApi({"vkmgn": [wall_item()]})
    c = VkCollector(api, db, cfg)
    c.prepare_sources()
    assert await c.poll_once() == 1
    assert await c.poll_once() == 0
    assert db.count_raw_posts() == 1


async def test_empty_posts_are_not_stored(db, cfg):
    api = FakeApi({"vkmgn": [wall_item(text="", attachments=[])]})
    c = VkCollector(api, db, cfg)
    c.prepare_sources()
    assert await c.poll_once() == 0


async def test_unavailable_source_is_recorded_and_others_still_collected(db, cfg):
    """Критерий приёмки: поллинг переживает недоступность источника."""
    class HalfBrokenApi(FakeApi):
        async def wall_get_batch(self, domains, count=20):
            return {d: (VkError(15, "Access denied") if d == "vkmgn" else [wall_item(post_id=hash(d) % 1000)])
                    for d in domains}

    c = VkCollector(HalfBrokenApi({}), db, cfg)
    specs = c.prepare_sources()
    assert await c.poll_once() >= 1                     # остальные паблики собраны

    broken = db.conn.execute(
        "SELECT last_error FROM sources WHERE key='vkmgn' AND source_type='vk'").fetchone()
    assert "Access denied" in broken["last_error"]
    healthy = next(s for s in specs if s.key != "vkmgn")
    ok = db.conn.execute(
        "SELECT last_error, last_polled_at FROM sources WHERE key=? AND source_type='vk'",
        (healthy.key,)).fetchone()
    assert ok["last_error"] is None and ok["last_polled_at"]


async def test_transport_failure_of_whole_batch_does_not_crash_poll(db, cfg):
    class DeadApi:
        async def wall_get_batch(self, domains, count=20):
            raise OSError("сеть отвалилась")

    c = VkCollector(DeadApi(), db, cfg)
    c.prepare_sources()
    assert await c.poll_once() == 0        # ошибка проглочена, процесс жив


async def test_posts_are_marked_as_polled_not_live(db, cfg):
    """Метка способа захвата (как в фазе 1) — чтобы замер приёмки не путал поллинг с live."""
    api = FakeApi({"vkmgn": [wall_item()]})
    c = VkCollector(api, db, cfg)
    c.prepare_sources()
    await c.poll_once()
    assert db.conn.execute("SELECT via FROM raw_posts").fetchone()["via"] == "poll"
