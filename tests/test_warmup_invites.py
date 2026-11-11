"""Вступление в приватные каналы по инвайтам (решение ревью §15).

Инвайты сняты с TGStat и лежат в sources.yaml. Порядок, заданный ревью:
  * не отдельной очередью, а через прогрев-механику — в общую дневную квоту,
    вперемешку с публичными подписками (ImportChatInvite для антиспама
    чувствительнее, чем JoinChannel);
  * не раньше чем через 24 ч после перелогина: свежая сессия плюс пачка
    инвайтов — это профиль спамера;
  * протух основной инвайт → пробуем запасной → статус dead.
"""
from datetime import datetime, timedelta, timezone

import pytest
from telethon.errors import InviteHashExpiredError

from tgtraff.config import SourceSpec, load_config
from tgtraff.db import Database
from tgtraff.warmup import WarmupPlanner, invite_hash

PUBLIC = SourceSpec(city="cherepovets", source_type="tg", key="example_channel_7", priority="high")
PRIVATE = SourceSpec(city="cherepovets", source_type="tg", query="Череповец 360°", status="resolve",
                     priority="high", invite="https://t.me/+Q9efYNJk8TFlN2My",
                     invite_fallback="https://t.me/joinchat/EJ9L3TTeDPI1MWVi")


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "w.db")
    yield d
    d.close()


@pytest.fixture
def cfg():
    return load_config()


def planner(db, cfg):
    return WarmupPlanner(db, cfg.warmup)


def hours_ago(h):
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


# ---- разбор инвайта ----

@pytest.mark.parametrize("url,expected", [
    ("https://t.me/+Q9efYNJk8TFlN2My", "Q9efYNJk8TFlN2My"),
    ("https://t.me/joinchat/EJ9L3TTeDPI1MWVi", "EJ9L3TTeDPI1MWVi"),
    ("t.me/+abc123", "abc123"),
    ("https://t.me/example_channel_7", None),          # публичный канал, не инвайт
    (None, None),
])
def test_invite_hash_is_taken_from_any_link_form(url, expected):
    assert invite_hash(url) == expected


# ---- очередь ----

def test_private_channel_with_invite_gets_queued(db, cfg):
    """Раньше resolve-записи в прогрев не попадали: у них нет username.
    С инвайтом попадать должны — вступление и есть способ их прочитать."""
    planner(db, cfg).enqueue([PUBLIC, PRIVATE])
    targets = {r["target"]: r for r in db.conn.execute("SELECT * FROM warmup_state")}
    assert "example_channel_7" in targets
    assert PRIVATE.invite in targets
    assert targets[PRIVATE.invite]["kind"] == "invite"
    assert targets["example_channel_7"]["kind"] == "username"


def test_resolve_entry_without_invite_is_not_queued(db, cfg):
    """Нечем вступать — нечего и ставить в очередь."""
    orphan = SourceSpec(city="cherepovets", source_type="tg", query="без инвайта", status="resolve")
    planner(db, cfg).enqueue([orphan])
    assert db.conn.execute("SELECT COUNT(*) c FROM warmup_state").fetchone()["c"] == 0


# ---- карантин свежей сессии ----

def test_invites_are_held_back_for_24h_after_relogin(db, cfg):
    """Свежая сессия + пачка вступлений по инвайтам = профиль спамера."""
    db.set_meta("session_created_at", hours_ago(2))
    p = planner(db, cfg)
    p.enqueue([PUBLIC, PRIVATE])

    day = p.today()
    offered = [p.next_target(day)["target"] for _ in range(1)]
    assert offered == ["example_channel_7"]              # публичная подписка идёт, инвайт ждёт


def test_invites_open_up_after_the_quarantine(db, cfg):
    db.set_meta("session_created_at", hours_ago(30))
    p = planner(db, cfg)
    p.enqueue([PRIVATE])
    assert p.next_target(p.today())["target"] == PRIVATE.invite


def test_without_relogin_mark_invites_are_allowed(db, cfg):
    """Отметки нет — значит сессия старая (перелогин был до появления отметки)."""
    p = planner(db, cfg)
    p.enqueue([PRIVATE])
    assert p.next_target(p.today()) is not None


def test_quarantine_end_is_reported_for_the_operator(db, cfg):
    db.set_meta("session_created_at", hours_ago(10))
    p = planner(db, cfg)
    assert p.invites_open_at() is not None
    assert p.invites_open_in_hours() == pytest.approx(14, abs=0.2)


# ---- перемешивание с публичными подписками ----

def test_invites_do_not_monopolise_the_daily_quota(db, cfg):
    """Инвайты идут вперемешку с обычными подписками, а не пачкой подряд."""
    db.set_meta("session_created_at", hours_ago(48))
    p = planner(db, cfg)
    publics = [SourceSpec(city="cherepovets", source_type="tg", key=f"pub{i}") for i in range(6)]
    invites = [SourceSpec(city="cherepovets", source_type="tg", query=f"priv{i}", status="resolve",
                          invite=f"https://t.me/+hash{i}") for i in range(3)]
    p.enqueue(publics + invites)

    order = [r["kind"] for r in db.warmup_pending(limit=20)]
    assert order.count("invite") == 3
    assert order[:3] != ["invite", "invite", "invite"], "инвайты не должны идти пачкой подряд"


# ---- само вступление ----

class FakeClient:
    """Считает запросы и умеет падать заданной ошибкой на конкретном хэше."""

    def __init__(self, fail_on=None, already=()):
        self.requests = []
        self.fail_on = fail_on or {}
        self.already = set(already)

    async def __call__(self, request):
        h = getattr(request, "hash", None)
        self.requests.append(h)
        if h in self.fail_on:
            raise self.fail_on[h]
        if h in self.already:
            from telethon.errors import UserAlreadyParticipantError
            raise UserAlreadyParticipantError(request=None)
        class Result:
            chats = [type("C", (), {"id": 123})()]
        return Result()

    async def get_entity(self, key):
        raise AssertionError("для инвайта get_entity не нужен")


async def join(db, cfg, client, spec=PRIVATE):
    from tgtraff.warmup import _join_one
    db.set_meta("session_created_at", hours_ago(48))
    p = planner(db, cfg)
    p.enqueue([spec])
    target = p.next_target(p.today())
    await _join_one(client, db, target, p.today(), p, {target["source_id"]: spec})
    return db.conn.execute("SELECT * FROM warmup_state WHERE source_id=?",
                           (target["source_id"],)).fetchone()


async def test_joins_private_channel_by_invite_hash(db, cfg):
    client = FakeClient()
    row = await join(db, cfg, client)
    assert client.requests == ["Q9efYNJk8TFlN2My"]
    assert row["status"] == "joined"
    assert db.get_source(row["source_id"])["joined_at"]


async def test_expired_invite_falls_back_to_the_spare_link(db, cfg):
    """У части каналов ревью сняло по два инвайта — протухший основной не повод сдаваться."""
    client = FakeClient(fail_on={"Q9efYNJk8TFlN2My": InviteHashExpiredError(request=None)})
    row = await join(db, cfg, client)
    assert client.requests == ["Q9efYNJk8TFlN2My", "EJ9L3TTeDPI1MWVi"]
    assert row["status"] == "joined"


async def test_both_invites_dead_marks_the_source_dead(db, cfg):
    """Решение ревью §15: не сработал ни один инвайт — статус dead, не бесконечные ретраи."""
    client = FakeClient(fail_on={
        "Q9efYNJk8TFlN2My": InviteHashExpiredError(request=None),
        "EJ9L3TTeDPI1MWVi": InviteHashExpiredError(request=None),
    })
    row = await join(db, cfg, client)
    assert row["status"] == "dead"
    assert db.warmup_pending() == []          # из очереди выбыл


async def test_already_participant_is_not_a_failure(db, cfg):
    client = FakeClient(already=["Q9efYNJk8TFlN2My"])
    row = await join(db, cfg, client)
    assert row["status"] == "already_member"


async def test_join_request_awaiting_approval_is_not_a_dead_invite(db, cfg):
    """У части приватных каналов вступление идёт через заявку: Telegram отвечает
    InviteRequestSent. Это «ждём одобрения», а не «инвайт мёртв» — запасную ссылку
    дёргать нельзя (уйдёт вторая заявка), и источник не dead."""
    from telethon.errors import InviteRequestSentError
    client = FakeClient(fail_on={"Q9efYNJk8TFlN2My": InviteRequestSentError(request=None)})

    row = await join(db, cfg, client)

    assert client.requests == ["Q9efYNJk8TFlN2My"]      # запасной инвайт не трогали
    assert row["status"] == "requested"
    assert db.get_source(row["source_id"])["status"] != "dead"
