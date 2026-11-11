"""Поллинг-фолбэк ТГ на время прогрева (решение ревью фазы 1, QUESTIONS §8).

`NewMessage` приходит только по каналам, в которых юзербот состоит, а плавный прогрев
подписывает 5-10 каналов в день. Пока он идёт, неподписанные источники читаются
поллингом: норматив доставки — ≤ 6 мин (против ≤ 1 мин у подписанных).

Модуль временный по построению: источник выпадает из опроса сразу после подписки,
а когда подписаны все — фолбэк выключает сам себя.
"""
from datetime import datetime, timedelta, timezone

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.types import Message, PeerChannel

from tgtraff.collector.tg_polling import PollingFallback
from tgtraff.collector.tg_collector import RuntimeSource
from tgtraff.config import SourceSpec, load_config
from tgtraff.db import Database

A = SourceSpec(city="cherepovets", source_type="tg", key="example_channel_7", priority="high")
B = SourceSpec(city="cherepovets", source_type="tg", key="dtp_che35", priority="high")


def msg(mid, minutes_ago=1, text="новость"):
    return Message(id=mid, peer_id=PeerChannel(channel_id=1), message=text,
                   date=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))


class FakeClient:
    def __init__(self, per_source=None, fail=None):
        self.per_source = per_source or {}
        self.fail = fail or {}
        self.asked = []

    async def iter_messages(self, entity, min_id=0, limit=None, **kw):
        key = getattr(entity, "username", entity)
        self.asked.append(key)
        if key in self.fail:
            raise self.fail[key]
        for m in sorted(self.per_source.get(key, []), key=lambda m: m.id, reverse=True)[:limit]:
            if m.id > min_id:
                yield m


def recorder(log):
    """Подменяет паузу: тест не спит, но видит, сколько бы проспал."""
    async def sleep(seconds):
        log.append(seconds)
    return sleep


class Entity:
    def __init__(self, username):
        self.username = username
        self.id = abs(hash(username)) % 10**9


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "p.db")
    yield d
    d.close()


@pytest.fixture
def cfg():
    return load_config()


def fallback(db, cfg, client, specs=(A, B), joined=()):
    fb = PollingFallback(client, db, cfg)
    for spec in specs:
        source_id = db.upsert_source(spec)
        if spec.key in joined:
            db.mark_source_joined(source_id)
        fb.register(RuntimeSource(spec=spec, source_id=source_id, entity=Entity(spec.key)))
    return fb


# ---- кого опрашиваем ----

def test_polls_only_sources_we_are_not_subscribed_to(db, cfg):
    fb = fallback(db, cfg, FakeClient(), joined=("example_channel_7",))
    assert [rs.spec.key for rs in fb.targets()] == ["dtp_che35"]


def test_source_leaves_the_poll_as_soon_as_warmup_subscribes_it(db, cfg):
    """Подписался — дальше он приходит событием, второй канал доставки не нужен."""
    fb = fallback(db, cfg, FakeClient())
    assert len(fb.targets()) == 2
    db.mark_source_joined(fb.sources[0].source_id)
    assert [rs.spec.key for rs in fb.targets()] == ["dtp_che35"]


def test_fallback_is_finished_when_everything_is_subscribed(db, cfg):
    fb = fallback(db, cfg, FakeClient(), joined=("example_channel_7", "dtp_che35"))
    assert fb.targets() == []
    assert fb.is_finished is True


# ---- сбор ----

async def test_poll_stores_new_posts_marked_as_poll(db, cfg):
    client = FakeClient({"example_channel_7": [msg(10), msg(11)]})
    fb = fallback(db, cfg, client, specs=(A,))

    assert await fb.poll_once() == 2

    rows = db.conn.execute("SELECT external_id, via FROM raw_posts ORDER BY external_id").fetchall()
    assert [(r["external_id"], r["via"]) for r in rows] == [("10", "poll"), ("11", "poll")]


async def test_second_poll_takes_only_what_appeared_since(db, cfg):
    client = FakeClient({"example_channel_7": [msg(10)]})
    fb = fallback(db, cfg, client, specs=(A,))
    assert await fb.poll_once() == 1

    client.per_source["example_channel_7"].append(msg(11))
    assert await fb.poll_once() == 1
    assert db.count_raw_posts() == 2


async def test_subscribed_source_is_not_polled_at_all(db, cfg):
    client = FakeClient({"example_channel_7": [msg(10)]})
    fb = fallback(db, cfg, client, specs=(A,), joined=("example_channel_7",))
    assert await fb.poll_once() == 0
    assert client.asked == []


async def test_broken_source_does_not_stop_the_others(db, cfg):
    """Критерий приёмки фазы 2: поллинг переживает недоступность источника."""
    client = FakeClient({"dtp_che35": [msg(5)]}, fail={"example_channel_7": OSError("канал недоступен")})
    fb = fallback(db, cfg, client)

    assert await fb.poll_once() == 1

    err = db.conn.execute("SELECT last_error FROM sources WHERE key='example_channel_7'").fetchone()["last_error"]
    assert "канал недоступен" in err


async def test_flood_wait_pauses_instead_of_crashing(db, cfg):
    """Аккаунт молодой: FloodWait — сигнал притормозить, а не падать."""
    slept = []
    client = FakeClient(fail={"example_channel_7": FloodWaitError(request=None)})
    client.fail["example_channel_7"].seconds = 30
    fb = fallback(db, cfg, client, specs=(A,))
    fb.sleep = recorder(slept)

    assert await fb.poll_once() == 0
    assert slept and slept[0] >= 30


async def test_polling_is_paced_in_batches(db, cfg):
    """Полсотни запросов подряд с молодого аккаунта — ровно тот всплеск,
    ради которого написан плавный прогрев. Опрашиваем пачками с паузой."""
    specs = [SourceSpec(city="cherepovets", source_type="tg", key=f"src{i}") for i in range(10)]
    pauses = []
    fb = fallback(db, cfg, FakeClient(), specs=specs)
    fb.sleep = recorder(pauses)

    await fb.poll_once()

    assert len(pauses) == (10 // cfg.poll_fallback.batch_size) - (1 if 10 % cfg.poll_fallback.batch_size == 0 else 0)
    assert all(p == cfg.poll_fallback.batch_pause_sec for p in pauses)


async def test_delivery_target_fits_the_warmup_norm(db, cfg):
    """Норматив QUESTIONS §8: неподписанные источники — не реже раза в 6 минут."""
    assert cfg.poll_fallback.interval_max_sec <= 6 * 60
    assert cfg.poll_fallback.interval_min_sec >= 3 * 60


# ---- список целей живёт в коллекторе: прогрев добавляет источники на ходу ----

async def test_fallback_picks_up_sources_added_after_start(db, cfg):
    """Коллектор подхватывает новые источники без рестарта (фаза 1) — фолбэк
    должен видеть их тем же списком, а не своей копией, снятой при старте."""
    registry = {}
    fb = PollingFallback(FakeClient({"example_channel_7": [msg(10)]}), db, cfg,
                         source_provider=lambda: registry.values())
    assert fb.targets() == []

    rs = RuntimeSource(spec=A, source_id=db.upsert_source(A), entity=Entity(A.key))
    registry[rs.source_id] = rs

    assert [t.spec.key for t in fb.targets()] == ["example_channel_7"]
    assert await fb.poll_once() == 1
