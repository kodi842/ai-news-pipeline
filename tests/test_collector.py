"""TG-коллектор: запись постов и гарантии «рестарт не теряет и не дублирует»."""
import hashlib
from datetime import datetime, timezone

import pytest
from telethon.tl.types import Message, MessageMediaPhoto, PeerChannel, Photo

from tgtraff.collector.tg_collector import RuntimeSource, TgCollector
from tgtraff.config import SourceSpec, load_config
from tgtraff.db import Database

TG = SourceSpec(city="cherepovets", source_type="tg", key="example_channel_7", priority="high")
REGIONAL = SourceSpec(city="magnitogorsk", source_type="tg_regional", key="news_74ru", city_filter="магнитогорск")


def msg(mid, text="новость", media=None):
    return Message(id=mid, peer_id=PeerChannel(channel_id=1),
                   date=datetime(2026, 8, 28, 7, 0, tzinfo=timezone.utc), message=text, media=media)


def photo():
    return MessageMediaPhoto(photo=Photo(id=1, access_hash=1, file_reference=b"",
                                         date=datetime.now(timezone.utc), sizes=[], dc_id=2))


class FakeClient:
    """Заглушка сети Telegram: отдаёт заранее заданные сообщения и «скачивает» медиа в файл."""

    def __init__(self, messages=(), fail_download=False):
        self.messages = {m.id: m for m in messages}
        self.fail_download = fail_download
        self.downloads = []

    async def get_messages(self, entity, ids=None, **kw):
        if isinstance(ids, list):
            return [self.messages.get(i) for i in ids]
        return self.messages.get(ids)

    async def download_media(self, message, file=None, **kw):
        if self.fail_download:
            raise OSError("сеть отвалилась")
        self.downloads.append(message.id)
        path = f"{file}.jpg"
        with open(path, "wb") as fh:
            fh.write(b"picture-bytes-%d" % message.id)
        return path


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def runtime(db, spec=TG):
    return RuntimeSource(spec=spec, source_id=db.upsert_source(spec), entity=None)


# ---- запись поста ----

def test_stores_message_as_raw_post(db, cfg):
    c = TgCollector(FakeClient(), db, cfg)
    rs = runtime(db)
    res = c.store_message(rs, msg(10, "Горел склад"))
    assert res.is_new
    row = db.conn.execute("SELECT * FROM raw_posts WHERE id=?", (res.id,)).fetchone()
    assert row["text"] == "Горел склад"
    assert row["source_type"] == "tg"
    assert row["url"] == "https://t.me/example_channel_7/10"


def test_same_message_twice_is_not_duplicated(db, cfg):
    c = TgCollector(FakeClient(), db, cfg)
    rs = runtime(db)
    assert c.store_message(rs, msg(10)).is_new is True
    assert c.store_message(rs, msg(10)).is_new is False
    assert db.count_raw_posts() == 1


def test_media_post_creates_pending_media_row(db, cfg):
    c = TgCollector(FakeClient(), db, cfg)
    res = c.store_message(runtime(db), msg(11, "пожар", media=photo()))
    pending = db.pending_media()
    assert [(p.raw_post_id, p.kind) for p in pending] == [(res.id, "photo")]


def test_regional_message_without_city_is_stored_filtered_without_media(db, cfg):
    c = TgCollector(FakeClient(), db, cfg)
    res = c.store_message(runtime(db, REGIONAL), msg(12, "В Челябинске развязка", media=photo()))
    row = db.conn.execute("SELECT * FROM raw_posts WHERE id=?", (res.id,)).fetchone()
    assert row["filtered_out"] == 1
    assert db.pending_media() == []


# ---- догон и рестарт ----

def test_catchup_min_id_is_zero_for_new_source(db, cfg):
    c = TgCollector(FakeClient(), db, cfg)
    assert c.catchup_min_id(runtime(db)) == 0


def test_catchup_min_id_is_last_collected_message(db, cfg):
    c = TgCollector(FakeClient(), db, cfg)
    rs = runtime(db)
    for i in (7, 42, 13):
        c.store_message(rs, msg(i))
    assert c.catchup_min_id(rs) == 42


def test_restart_does_not_lose_or_duplicate_posts(tmp_path, cfg):
    """Процесс собрал 1-5, упал; за простой вышли 6-8; после рестарта в БД ровно 1-8 по разу."""
    path = tmp_path / "t.db"
    db1 = Database(path)
    c1 = TgCollector(FakeClient(), db1, cfg)
    rs1 = runtime(db1)
    for i in range(1, 6):
        c1.store_message(rs1, msg(i))
    db1.close()

    db2 = Database(path)
    c2 = TgCollector(FakeClient(), db2, cfg)
    rs2 = RuntimeSource(spec=TG, source_id=db2.upsert_source(TG), entity=None)
    assert c2.catchup_min_id(rs2) == 5                       # догоняем строго с 6-го
    # догон отдаёт всё после min_id, событийный поток мог продублировать 4-5 — не важно
    for i in (4, 5, 6, 7, 8):
        c2.store_message(rs2, msg(i))
    ids = [r["external_id"] for r in db2.conn.execute("SELECT external_id FROM raw_posts ORDER BY CAST(external_id AS INT)")]
    assert ids == ["1", "2", "3", "4", "5", "6", "7", "8"]
    db2.close()


# ---- скачивание медиа ----

async def test_downloads_pending_media_and_records_hash(db, cfg, tmp_path):
    m = msg(20, "пожар", media=photo())
    client = FakeClient([m])
    c = TgCollector(client, db, cfg, media_dir=tmp_path)
    rs = runtime(db)
    c.register_source(rs)
    post = c.store_message(rs, m)

    assert await c.download_pending_media() == 1
    row = db.conn.execute("SELECT * FROM raw_media WHERE raw_post_id=?", (post.id,)).fetchone()
    assert row["status"] == "done"
    assert row["file_path"].endswith(".jpg")
    assert row["file_size"] == len(b"picture-bytes-20")
    assert row["sha256"] == hashlib.sha256(b"picture-bytes-20").hexdigest()
    assert db.conn.execute("SELECT media_state FROM raw_posts WHERE id=?", (post.id,)).fetchone()[0] == "done"


async def test_media_saved_under_city_and_source_folder(db, cfg, tmp_path):
    m = msg(21, "пожар", media=photo())
    c = TgCollector(FakeClient([m]), db, cfg, media_dir=tmp_path)
    rs = runtime(db)
    c.register_source(rs)
    c.store_message(rs, m)
    await c.download_pending_media()
    path = db.conn.execute("SELECT file_path FROM raw_media").fetchone()[0]
    assert "cherepovets" in path and "example_channel_7" in path and "21" in path


async def test_failed_download_stays_in_queue_for_retry(db, cfg, tmp_path):
    m = msg(22, "пожар", media=photo())
    c = TgCollector(FakeClient([m], fail_download=True), db, cfg, media_dir=tmp_path)
    rs = runtime(db)
    c.register_source(rs)
    c.store_message(rs, m)

    assert await c.download_pending_media() == 0
    still = db.pending_media()
    assert len(still) == 1 and still[0].attempts == 1


async def test_media_pending_after_restart_is_downloaded(tmp_path, cfg):
    """Упали между записью поста и скачиванием — после рестарта медиа докачивается."""
    m = msg(23, "пожар", media=photo())
    path = tmp_path / "t.db"
    db1 = Database(path)
    c1 = TgCollector(FakeClient([m]), db1, cfg, media_dir=tmp_path)
    rs1 = runtime(db1)
    c1.store_message(rs1, m)          # медиа поставлено в очередь, но не скачано
    db1.close()

    db2 = Database(path)
    c2 = TgCollector(FakeClient([m]), db2, cfg, media_dir=tmp_path)
    c2.register_source(RuntimeSource(spec=TG, source_id=db2.upsert_source(TG), entity=None))
    assert await c2.download_pending_media() == 1
    assert db2.conn.execute("SELECT status FROM raw_media").fetchone()[0] == "done"
    db2.close()


# ---- маршрутизация события в источник ----

def test_event_is_routed_to_its_source_by_peer_id(db, cfg):
    """event.chat_id приходит в формате -100<channel_id> — источник должен находиться."""
    from telethon.tl.types import Channel
    from telethon.utils import get_peer_id

    channel = Channel(id=1234567890, title="ГородЧе", photo=None, date=None,
                      access_hash=1, username="example_channel_7", broadcast=True)
    c = TgCollector(FakeClient(), db, cfg)
    rs = RuntimeSource(spec=TG, source_id=db.upsert_source(TG), entity=channel)
    c.register_source(rs)

    class Ev:
        chat_id = get_peer_id(channel)
        message = msg(31)

    assert c._source_for_event(Ev()) is rs


def test_event_from_unknown_chat_is_ignored(db, cfg):
    c = TgCollector(FakeClient(), db, cfg)
    c.register_source(RuntimeSource(spec=TG, source_id=db.upsert_source(TG), entity=None))

    class Ev:
        chat_id = -1009999999999
        message = msg(32)

    assert c._source_for_event(Ev()) is None


# ---- источники добавляются на ходу (прогрев идёт параллельно 4 дня) ----

class ResolvingClient(FakeClient):
    """Клиент, который «разрешает» только каналы из allowed — остальные недоступны."""

    def __init__(self, allowed, messages=()):
        super().__init__(messages)
        self.allowed = set(allowed)

    async def get_entity(self, key):
        if key not in self.allowed:
            raise ValueError(f"нет доступа к {key}")
        from telethon.tl.types import Channel
        return Channel(id=abs(hash(key)) % 10**9, title=key, photo=None, date=None,
                       access_hash=1, username=key, broadcast=True)


async def test_prepare_sources_skips_unavailable(db, cfg):
    """Источник, на который ещё не подписаны, пропускается — коллектор из-за него не падает."""
    c = TgCollector(ResolvingClient(allowed={"example_channel_7"}), db, cfg)
    c.cfg = cfg
    prepared = await c.prepare_sources()
    assert [rs.spec.key for rs in prepared] == ["example_channel_7"]


async def test_newly_joined_source_is_picked_up_without_restart(db, cfg):
    client = ResolvingClient(allowed={"example_channel_7"})
    c = TgCollector(client, db, cfg)
    assert len(await c.prepare_sources()) == 1

    client.allowed.add("dtp_che35")            # прогрев подписался на второй канал
    added = await c.refresh_sources()
    assert [rs.spec.key for rs in added] == ["dtp_che35"]
    assert len(c.sources) == 2


async def test_refresh_does_not_re_register_known_sources(db, cfg):
    client = ResolvingClient(allowed={"example_channel_7"})
    c = TgCollector(client, db, cfg)
    await c.prepare_sources()
    assert await c.refresh_sources() == []
    assert len(c.sources) == 1


async def test_collector_survives_start_with_no_available_sources(db, cfg):
    """Юзербот только прогревается — источников ещё нет. Это не повод падать."""
    c = TgCollector(ResolvingClient(allowed=set()), db, cfg)
    assert await c.prepare_sources() == []


# ---- догон ограничен сутками (решение ревью фазы 1, QUESTIONS §5) ----

class HistoryClient(FakeClient):
    """Отдаёт историю канала от свежих к старым, как iter_messages Telethon."""

    def __init__(self, messages):
        super().__init__(messages)
        self.ordered = sorted(messages, key=lambda m: m.id, reverse=True)

    async def iter_messages(self, entity, min_id=0, limit=None, **kw):
        seen = 0
        for m in self.ordered:
            if m.id <= min_id:
                break
            if limit is not None and seen >= limit:
                break
            seen += 1
            yield m


def aged(mid, hours_ago, text="новость"):
    from datetime import timedelta
    return Message(id=mid, peer_id=PeerChannel(channel_id=1),
                   date=datetime.now(timezone.utc) - timedelta(hours=hours_ago), message=text)


async def test_catchup_stops_at_24h_boundary(db, cfg):
    """Новости старше суток для ЧП-ленты мертвы — глубже не копаем."""
    msgs = [aged(10, 1), aged(9, 5), aged(8, 23), aged(7, 30), aged(6, 100)]
    c = TgCollector(HistoryClient(msgs), db, cfg)
    rs = runtime(db)
    assert await c.catchup_source(rs) == 3                      # 1ч, 5ч, 23ч
    ids = {r[0] for r in db.conn.execute("SELECT external_id FROM raw_posts")}
    assert ids == {"10", "9", "8"}


async def test_catchup_limit_still_caps_a_busy_source(db, cfg):
    """Предохранитель на случай очень активного источника внутри суток."""
    msgs = [aged(i, 1) for i in range(1, 40)]
    cfg_small = load_config()
    object.__setattr__(cfg_small.collector, "catchup_limit", 5)
    c = TgCollector(HistoryClient(msgs), db, cfg_small)
    assert await c.catchup_source(runtime(db)) == 5


async def test_catchup_still_starts_after_last_seen_message(db, cfg):
    msgs = [aged(10, 1), aged(9, 2), aged(8, 3)]
    c = TgCollector(HistoryClient(msgs), db, cfg)
    rs = runtime(db)
    c.store_message(rs, aged(9, 2))                             # 9 уже собран
    assert await c.catchup_source(rs) == 1                      # добираем только 10


# ---- фаза 2: очередь медиа общая, качальщики разные ----

async def test_telethon_worker_leaves_foreign_media_alone(db, cfg):
    """ВК-медиа качается обычным HTTP другим воркером. Telethon-воркер не должен
    ни пытаться его тянуть, ни помечать пропущенным — иначе оно потеряно навсегда."""
    from tgtraff.config import SourceSpec
    vk_spec = SourceSpec(city="magnitogorsk", source_type="vk", key="vkmgn")
    vk_id = db.upsert_source(vk_spec)
    vk_post = db.insert_raw_post(source_id=vk_id, source_type="vk", external_id="-1_2",
                                 published_at="2026-08-28T10:00:00+00:00", text="ДТП", has_media=1,
                                 media_state="pending")
    db.add_media(vk_post.id, ord=0, kind="photo", source_url="https://sun9.userapi.com/x.jpg")

    c = TgCollector(FakeClient(), db, cfg)
    assert await c.download_pending_media() == 0

    row = db.conn.execute("SELECT status FROM raw_media WHERE raw_post_id=?", (vk_post.id,)).fetchone()
    assert row["status"] == "pending"        # ждёт своего воркера, а не «пропущено»


# ---- честность метки live: события после разрыва связи — это догон, а не live ----

async def test_updates_missed_while_offline_are_marked_catchup(db, cfg):
    """После обрыва/рестарта Telethon добирает пропущенные апдейты через getDifference
    и отдаёт их тем же обработчиком NewMessage. Помечать их live нельзя: тогда замер
    «пост в базе за минуту» показывает часы лага там, где живой доставки не было."""
    c = TgCollector(FakeClient(), db, cfg)
    rs = runtime(db)
    c.register_source(rs)
    c._by_peer[777] = rs

    old = aged(50, hours_ago=3)          # опубликован до старта процесса
    await c.on_new_message(FakeEvent(chat_id=777, message=old))

    row = db.conn.execute("SELECT via FROM raw_posts WHERE external_id='50'").fetchone()
    assert row["via"] == "catchup"


async def test_message_published_after_start_is_live(db, cfg):
    c = TgCollector(FakeClient(), db, cfg)
    rs = runtime(db)
    c.register_source(rs)
    c._by_peer[777] = rs

    fresh = Message(id=51, peer_id=PeerChannel(channel_id=1), message="свежая",
                    date=datetime.now(timezone.utc))
    await c.on_new_message(FakeEvent(chat_id=777, message=fresh))

    assert db.conn.execute("SELECT via FROM raw_posts WHERE external_id='51'").fetchone()["via"] == "live"


class FakeEvent:
    def __init__(self, chat_id, message):
        self.chat_id = chat_id
        self.message = message
