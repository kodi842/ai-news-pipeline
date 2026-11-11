"""Конвейер фазы 4: кластер → relevance → rewriter → safety → драфт в базе.

Проверяется склейка узлов и то, что решения доходят до базы: отсеянный кластер
драфта не порождает, вердикт safety лежит в строке драфта, деньги записаны.
"""
import pytest

from tgtraff import drafts as store
from tgtraff.config import SourceSpec
from tgtraff.db import Database
from tgtraff.llm.client import LlmError
from tgtraff.pipeline import Phase4Pipeline, PipelineSettings

CITY = "novokuznetsk"

REWRITE = {
    "title": "На Кузнецком мосту столкнулись автобус и легковушка",
    "body": "Авария произошла утром. Пострадал водитель легковушки, его увезли в больницу.",
    "category": "dtp", "urgency": "flash", "district": None, "low_facts": False, "note": "",
}
FACTS = ("На Кузнецком мосту столкнулись автобус и легковушка. Пострадал водитель "
         "легковушки, его увезли в больницу. Движение затруднено.")


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


class Usage:
    input_tokens, output_tokens, cache_read, cache_write, cost_usd = 500, 100, 400, 0, 0.0005


class ScriptedClient:
    """Отвечает по назначению запроса: relevance / rewrite / safety.

    Ведёт себя как настоящий клиент: после успешного запроса зовёт `on_usage`
    — на этом крючке конвейер пишет потраченное в базу."""

    def __init__(self, **replies):
        self.replies = replies
        self.calls: list[str] = []
        self.on_usage = None
        self.settings = type("S", (), {"model": "claude-haiku-4-5"})()

    def ask_json(self, system, user, purpose="other", **kw):
        self.calls.append(purpose)
        reply = self.replies.get(purpose)
        if reply is None:
            raise AssertionError(f"незапланированный запрос {purpose}")
        if isinstance(reply, Exception):
            raise reply
        if self.on_usage:
            self.on_usage(purpose, "claude-haiku-4-5", Usage())

        class _R:
            model = "claude-haiku-4-5"
            cost_usd = Usage.cost_usd
            usage = Usage()
        return reply, _R()


def pipeline(db, client, **kw):
    return Phase4Pipeline(db, client, settings=PipelineSettings(**kw),
                          city_names={CITY: "Новокузнецк"})


def cluster(db, text=FACTS, key="chp_nvkz", media_path=None):
    sid = db.upsert_source(SourceSpec(city=CITY, source_type="tg", key=key))
    pid = db.insert_raw_post(source_id=sid, source_type="tg",
                             external_id=str(abs(hash(text)) % 10**7),
                             published_at="2026-09-02T09:00:00+00:00", text=text,
                             has_media=1 if media_path else 0,
                             media_state="pending" if media_path else "none").id
    if media_path:
        mid = db.add_media(pid, 0, "photo")
        db.conn.execute("UPDATE raw_media SET status='done', file_path=?, watermark='none'"
                        " WHERE id=?", (media_path, mid))
    cur = db.conn.execute(
        "INSERT INTO clusters (city, status, lead_raw_post_id, size, first_seen_at, last_seen_at)"
        " VALUES (?, 'open', ?, 1, '2026-09-02T09:00:00+00:00','2026-09-02T09:00:00+00:00')",
        (CITY, pid))
    db.conn.execute("INSERT INTO cluster_members (cluster_id, raw_post_id, reason)"
                    " VALUES (?,?,'new')", (cur.lastrowid, pid))
    return cur.lastrowid


def full_client(**over):
    replies = {"relevance": {"verdict": "news"}, "rewrite": REWRITE,
               "safety": {"verdict": "safe", "categories": [], "reason": ""}}
    replies.update(over)
    return ScriptedClient(**replies)


# --- счастливый путь --------------------------------------------------------

def test_cluster_becomes_a_draft_in_the_database(db):
    cid = cluster(db)
    result = pipeline(db, full_client()).process(cid)

    assert result.status == "draft"
    row = store.draft_for_cluster(db, cid)
    assert row["title"] == REWRITE["title"]
    assert row["category"] == "dtp"
    assert row["safety_verdict"] == "safe"
    assert row["relevance"] == "news"
    assert store.decode_list(row["sources"]) == ["chp_nvkz"]


def test_media_choice_is_saved_with_the_draft(db):
    cid = cluster(db, media_path="/m/clean.jpg")
    pipeline(db, full_client()).process(cid)
    row = store.draft_for_cluster(db, cid)
    assert row["media_path"] == "/m/clean.jpg"
    assert row["media_kind"] == "photo"


def test_money_is_written_to_llm_usage(db):
    pipeline(db, full_client()).process(cluster(db))
    total = store.usage_total(db)
    assert total["calls"] == 3               # relevance + rewrite + safety
    assert total["cost_usd"] > 0
    assert {r["purpose"] for r in store.usage_by_purpose(db)} == {
        "relevance", "rewrite", "safety"}


# --- отсев ------------------------------------------------------------------

def test_ad_cluster_is_dropped_without_a_draft(db):
    cid = cluster(db, text="Скидка 20% на теплицы! erid:2Vtzqxkmj5d")
    client = full_client()
    result = pipeline(db, client).process(cid)

    assert result.status == "dropped"
    assert result.relevance.verdict == "ad"
    assert store.draft_for_cluster(db, cid) is None
    assert "rewrite" not in client.calls          # за рерайт рекламы не платим
    row = db.conn.execute("SELECT relevance FROM clusters WHERE id=?", (cid,)).fetchone()
    assert row["relevance"] == "ad"


def test_processed_cluster_is_not_processed_twice(db):
    cid = cluster(db)
    p = pipeline(db, full_client())
    p.process(cid)
    again = p.process(cid)
    assert again.status == "skipped"


# --- safety -----------------------------------------------------------------

def test_manual_only_verdict_lands_in_the_draft(db):
    client = full_client(safety={"verdict": "manual_only", "categories": ["военное"],
                                 "reason": "БПЛА"})
    cid = cluster(db)
    pipeline(db, client).process(cid)
    row = store.draft_for_cluster(db, cid)
    assert row["safety_verdict"] == "manual_only"
    assert store.decode_list(row["safety_reasons"]) == ["военное"]


def test_rejected_draft_is_stored_but_marked(db):
    """Отклонённый safety драфт всё равно виден модератору — с вердиктом reject."""
    client = full_client(safety={"verdict": "reject", "categories": ["18+ жесть"]})
    cid = cluster(db)
    result = pipeline(db, client).process(cid)
    row = store.draft_for_cluster(db, cid)
    assert row["safety_verdict"] == "reject"
    assert row["status"] == "rejected"
    assert result.status == "draft"


def test_phase4_never_marks_a_draft_approved(db):
    """Автопубликации в фазе 4 нет ни при каком вердикте (спека §6)."""
    cid = cluster(db)
    pipeline(db, full_client()).process(cid)
    assert store.draft_for_cluster(db, cid)["status"] == "new"


# --- сбои -------------------------------------------------------------------

def test_rewriter_failure_leaves_cluster_for_the_next_run(db):
    cid = cluster(db)
    client = full_client(rewrite=LlmError("500 от API"))
    result = pipeline(db, client).process(cid)

    assert result.status == "error"
    assert store.draft_for_cluster(db, cid) is None
    row = db.conn.execute("SELECT relevance FROM clusters WHERE id=?", (cid,)).fetchone()
    assert row["relevance"] is None          # кластер вернётся в очередь


def test_run_once_walks_the_queue_and_counts(db):
    cluster(db, text=FACTS, key="a")
    cluster(db, text="Скидки! erid:2Vtzqxkmj5d", key="b")
    stats = pipeline(db, full_client()).run_once(city=CITY, limit=10)

    assert stats.drafts == 1
    assert stats.dropped == 1
    assert stats.processed == 2


# --- шаблонная обложка (ступень 3 каскада) ----------------------------------

def test_template_cover_is_rendered_and_saved_with_the_draft(db, tmp_path):
    cid = cluster(db)          # медиа в кластере нет → каскад просит обложку
    p = Phase4Pipeline(db, full_client(),
                       settings=PipelineSettings(cover_dir=str(tmp_path)),
                       city_names={CITY: "Новокузнецк"})
    p.process(cid)

    row = store.draft_for_cluster(db, cid)
    assert row["media_kind"] == "template"
    assert row["media_path"] and row["media_path"].endswith(".png")
    assert (tmp_path / row["media_path"].split("/")[-1]).exists()


# --- §28: повторное предложение после отклонения по низкой фактуре -----------

def test_cluster_rejected_for_low_facts_gets_a_second_draft_when_facts_arrive(db):
    """Решение ревью §28: отклонение по `low_facts` + заметный прирост фактуры
    (+2 источника) — кластер предлагается модератору заново."""
    from tgtraff.config import SourceSpec
    from tgtraff.moderation import actions

    cid = cluster(db)
    pipe = pipeline(db, full_client())
    pipe.process(cid)
    first = store.draft_for_cluster(db, cid)["id"]
    actions.reject(db, first, moderator_id=7, reason=actions.LOW_FACTS)

    for i in range(2):                              # кластеру пришли два источника
        sid = db.upsert_source(SourceSpec(city=CITY, source_type="tg", key=f"more{i}"))
        pid = db.insert_raw_post(source_id=sid, source_type="tg", external_id=f"m{i}",
                                 published_at="2026-09-02T10:00:00+00:00",
                                 text=FACTS + f" Уточнение {i}.").id
        db.conn.execute("INSERT INTO cluster_members (cluster_id, raw_post_id, reason)"
                        " VALUES (?,?,'semantic')", (cid, pid))

    stats = pipeline(db, full_client()).run_once()
    second = store.draft_for_cluster(db, cid)
    assert stats.drafts == 1
    assert second is not None and second["id"] != first
    assert db.conn.execute("SELECT status FROM drafts WHERE id=?",
                           (first,)).fetchone()["status"] == "superseded"


def test_cluster_rejected_as_not_news_stays_silent(db):
    from tgtraff.config import SourceSpec
    from tgtraff.moderation import actions

    cid = cluster(db)
    pipeline(db, full_client()).process(cid)
    first = store.draft_for_cluster(db, cid)["id"]
    actions.reject(db, first, moderator_id=7, reason=actions.NOT_NEWS)

    for i in range(3):
        sid = db.upsert_source(SourceSpec(city=CITY, source_type="tg", key=f"never{i}"))
        pid = db.insert_raw_post(source_id=sid, source_type="tg", external_id=f"n{i}",
                                 published_at="2026-09-02T10:00:00+00:00", text=FACTS).id
        db.conn.execute("INSERT INTO cluster_members (cluster_id, raw_post_id, reason)"
                        " VALUES (?,?,'semantic')", (cid, pid))

    assert pipeline(db, full_client()).run_once().drafts == 0
    assert store.draft_for_cluster(db, cid)["id"] == first


def test_district_from_the_rewriter_reaches_the_draft(db):
    """Хэштег района ставит publisher — значит, район должен доехать до строки."""
    cid = cluster(db)
    pipeline(db, full_client(rewrite={**REWRITE, "district": "Заводской район"})).process(cid)
    assert store.draft_for_cluster(db, cid)["district"] == "Заводской район"


def test_unavailable_model_aborts_the_whole_run_not_just_one_city(db):
    """Живой прогон на РФ-сервере: `api.anthropic.com` отдаёт 403 (гео-блок).

    Сбой ОДНОГО кластера проход переживает — следующий может пройти. Но если
    модель недоступна всем, перебор оставшихся кластеров (и следующих городов)
    бессмысленен: под таймером это тысячи мёртвых запросов в сутки. Такое
    поднимается наружу, а не гасится внутри прохода.
    """
    from tgtraff.llm.client import LlmUnavailable

    for i in range(3):
        cluster(db, text=f"{FACTS} Вариант {i}", key=f"src{i}")
    client = full_client(relevance=LlmUnavailable("модель недоступна (HTTP 403)"))

    with pytest.raises(LlmUnavailable):
        pipeline(db, client).run_once(city=CITY, limit=10)


def test_an_ordinary_llm_error_still_degrades_gracefully(db):
    """Обычный сбой ОДНОГО запроса (исчерпан бюджет, не разобрался ответ) —
    не авария: relevance откатывается на «оставляем в ленте», проход идёт
    дальше и драфт получается. Наружу такое не поднимается."""
    cluster(db, text=FACTS, key="a")
    client = full_client(relevance=LlmError("исчерпан бюджет запросов"))

    stats = pipeline(db, client).run_once(city=CITY, limit=10)
    assert stats.drafts == 1
    assert stats.errors == 0


# ---- «писать не из чего» — это решение, а не сбой -----------------------------
#
# Живой прогон на сервере: по кластеру из одного заголовка модель честно
# возвращает пустой драфт с пометкой low_facts и объяснением. Пайплайн считал
# это ОШИБКОЙ рерайта, кластер оставался в очереди — и под таймером
# перебирался заново каждые 5 минут. Хуже стоимости было то, что очередь
# сортируется от старых к новым: застрявшие кластеры занимали начало окна,
# и свежие новости до разбора не доходили вовсе.

EMPTY_DRAFT = {"title": "", "body": "", "category": "city", "urgency": "normal",
               "low_facts": True, "note": "в материале только заголовок, фактов нет"}


def test_cluster_without_facts_leaves_the_queue(db):
    cid = cluster(db, text="Итоги недели")
    client = full_client(rewrite=EMPTY_DRAFT)

    result = pipeline(db, client).process(cid)

    assert result.status == "low_facts"
    assert cid not in store.pending_clusters(db, city=CITY), \
        "кластер должен уйти из очереди, иначе его будут перебирать вечно"


def test_the_decision_is_visible_and_carries_the_reason(db):
    cid = cluster(db, text="Итоги недели")
    pipeline(db, full_client(rewrite=EMPTY_DRAFT)).process(cid)

    row = db.conn.execute(
        "SELECT status, reject_reason, notes FROM drafts WHERE cluster_id=?", (cid,)).fetchone()
    assert row["status"] == "rejected"
    assert row["reject_reason"] == "low_facts"
    assert "заголовок" in (row["notes"] or ""), "объяснение модели должно сохраниться"


def test_a_grown_cluster_is_offered_again(db):
    """§28 целиком: пришла фактура — кластер возвращается на рерайт. Механику
    берём готовую, ту же, что у отклонения модератором."""
    cid = cluster(db, text="Итоги недели")
    p = pipeline(db, full_client(rewrite=EMPTY_DRAFT))
    p.process(cid)

    for i in range(2):                            # +2 источника — заметный прирост
        sid = db.upsert_source(SourceSpec(city=CITY, source_type="tg", key=f"more{i}"))
        pid = db.insert_raw_post(source_id=sid, source_type="tg", external_id=f"m{i}",
                                 published_at="2026-09-02T10:00:00+00:00",
                                 text="Подробности: столкнулись автобус и легковушка").id
        db.conn.execute("INSERT INTO cluster_members (cluster_id, raw_post_id) VALUES (?,?)",
                        (cid, pid))
    db.conn.execute("UPDATE clusters SET size=3 WHERE id=?", (cid,))

    assert p.reoffer() == 1
    assert cid in store.pending_clusters(db, city=CITY)


def test_a_still_thin_cluster_is_not_offered_again(db):
    cid = cluster(db, text="Итоги недели")
    p = pipeline(db, full_client(rewrite=EMPTY_DRAFT))
    p.process(cid)

    assert p.reoffer() == 0
    assert cid not in store.pending_clusters(db, city=CITY)


def test_no_facts_clusters_are_visible_in_the_summary(db):
    """Восемь таких кластеров за проход не должны бесследно исчезать из сводки:
    отсев по фактуре — такой же отсев, как реклама и офтопик."""
    cluster(db, text="Итоги недели", key="a")
    stats = pipeline(db, full_client(rewrite=EMPTY_DRAFT)).run_once(city=CITY, limit=10)

    assert stats.dropped == 1
    assert stats.by_reason.get("low_facts") == 1
    assert stats.errors == 0
    assert "low_facts=1" in stats.line()
