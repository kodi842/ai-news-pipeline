"""Кластеризация ленты: два уровня дедупа и правило «драфт уже есть» (спека §3).

Модель здесь подставная — с точно заданными косинусами: иначе тест про порог
0.85 проверял бы не наш код, а настроение нейросети. Живая модель и калибровка
порога — в tests/test_dedup_embed.py и scripts/dedup_calibrate.py.
"""
import math

import numpy as np
import pytest

from tgtraff.config import SourceSpec
from tgtraff.db import Database
from tgtraff.dedup.cluster import Deduplicator
from tgtraff.dedup.settings import DedupSettings
from tgtraff.dedup.store import cluster_members, cluster_of
from tgtraff.normalize import normalized_feed

CITY = "magnitogorsk"


class FakeEmbedder:
    """Вектор задаётся углом: косинус пары текстов известен заранее.

    Тексты, для которых угол не задан, получают взаимно ортогональные векторы —
    «разные новости» по умолчанию."""

    name = "fake"
    dim = 16

    def __init__(self, angles: dict[str, float] | None = None):
        self.angles = angles or {}
        self.calls = 0
        self.encoded: list[str] = []
        self._extra: dict[str, int] = {}

    def _vector(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        if not text.strip():
            return vec
        for key, angle in self.angles.items():
            if key in text:
                rad = math.radians(angle)
                vec[0], vec[1] = math.cos(rad), math.sin(rad)
                return vec
        slot = self._extra.setdefault(text, 2 + len(self._extra) % (self.dim - 2))
        vec[slot] = 1.0
        return vec

    def encode(self, texts: list[str]) -> np.ndarray:
        self.calls += 1
        self.encoded.extend(texts)
        return np.vstack([self._vector(t) for t in texts]) if texts else np.zeros((0, self.dim))


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def source(db, key, source_type="tg", city=CITY):
    return db.upsert_source(SourceSpec(city=city, source_type=source_type, key=key))


def post(db, source_id, text, at="2026-09-01T10:00:00+00:00", source_type="tg",
         external_id=None, media_path=None, media_status="done", grouped_id=None):
    ext = external_id or str(abs(hash((source_id, text, at, grouped_id))) % 10**7)
    res = db.insert_raw_post(source_id=source_id, source_type=source_type, external_id=ext,
                             published_at=at, text=text, grouped_id=grouped_id,
                             has_media=1 if media_path else 0,
                             media_state="pending" if media_path else "none")
    if media_path:
        media_id = db.add_media(res.id, 0, "photo")
        db.conn.execute("UPDATE raw_media SET status=?, file_path=? WHERE id=?",
                        (media_status, media_path, media_id))
    return res.id


def feed(db, city=CITY):
    """Лента конвейера в хронологическом порядке — так её и обрабатывает дедуп."""
    return list(reversed(normalized_feed(db, city=city, limit=500)))


def run(db, embedder=None, settings=None, **overrides):
    settings = settings or DedupSettings(**overrides)
    dedup = Deduplicator(db, embedder or FakeEmbedder(), settings)
    return dedup, dedup.process_feed(feed(db))


# ---- уровень 1: точный текст ----

def test_same_text_from_two_sources_is_one_cluster(db):
    a, b = source(db, "example_channel_5"), source(db, "verstov_info", "vk")
    text = "В Магнитогорске сгорел склад на улице Ленина, пострадавших нет."
    p1 = post(db, a, text + "\n\nПодписаться")
    p2 = post(db, b, text + " #Магнитогорск", source_type="vk", at="2026-09-01T10:20:00+00:00")

    _, decisions = run(db)

    assert cluster_of(db.conn, p1) == cluster_of(db.conn, p2)
    assert [d.reason for d in decisions] == ["new", "exact_text"]
    assert decisions[1].similarity == 1.0


def test_short_identical_texts_do_not_merge_by_hash(db):
    """«Доброе утро» у двух пабликов — не одна новость (у текста нет хэша)."""
    a, b = source(db, "example_channel_5"), source(db, "podslushano", "vk")
    p1 = post(db, a, "Доброе утро!")
    p2 = post(db, b, "Доброе утро!", source_type="vk")

    run(db)

    assert cluster_of(db.conn, p1) != cluster_of(db.conn, p2)


# ---- уровень 2: семантика ----

def test_paraphrase_above_threshold_joins_cluster(db):
    a, b = source(db, "example_channel_5"), source(db, "mgorsk", "rss")
    p1 = post(db, a, "Пожар в доме на Ленина: жильцов эвакуировали")
    p2 = post(db, b, "На улице Ленина горел жилой дом, людей вывели из квартир",
              source_type="rss", at="2026-09-01T10:40:00+00:00")
    emb = FakeEmbedder({"Пожар в доме": 0.0, "горел жилой дом": 20.0})   # cos ≈ 0.94

    _, decisions = run(db, emb)

    assert cluster_of(db.conn, p1) == cluster_of(db.conn, p2)
    assert decisions[1].reason == "semantic"
    assert decisions[1].similarity == pytest.approx(math.cos(math.radians(20)), abs=1e-3)


def test_similar_but_below_threshold_stays_separate(db):
    a, b = source(db, "example_channel_5"), source(db, "mgorsk", "rss")
    p1 = post(db, a, "Пожар в доме на Ленина: жильцов эвакуировали")
    p2 = post(db, b, "В городе прошли учения пожарных расчётов",
              source_type="rss", at="2026-09-01T10:40:00+00:00")
    emb = FakeEmbedder({"Пожар в доме": 0.0, "учения пожарных": 35.0})   # cos ≈ 0.82

    run(db, emb)

    assert cluster_of(db.conn, p1) != cluster_of(db.conn, p2)


def test_threshold_is_a_config_parameter(db):
    """Спека §3: порог калибруется. Тот же материал при пороге 0.80 склеивается."""
    a, b = source(db, "example_channel_5"), source(db, "mgorsk", "rss")
    p1 = post(db, a, "Пожар в доме на Ленина: жильцов эвакуировали")
    p2 = post(db, b, "В городе прошли учения пожарных расчётов",
              source_type="rss", at="2026-09-01T10:40:00+00:00")
    emb = FakeEmbedder({"Пожар в доме": 0.0, "учения пожарных": 35.0})

    run(db, emb, threshold=0.80, centroid_margin=0.10)

    assert cluster_of(db.conn, p1) == cluster_of(db.conn, p2)


def test_very_short_text_is_not_merged_by_semantics(db):
    """У короткого текста вектор случайный — семантике его доверять нельзя."""
    a, b = source(db, "example_channel_5"), source(db, "mgorsk", "rss")
    p1 = post(db, a, "Пожар на Ленина")
    p2 = post(db, b, "Пожар на Ленина, 12", source_type="rss",
              at="2026-09-01T10:40:00+00:00")
    emb = FakeEmbedder({"Пожар на Ленина": 0.0})

    run(db, emb)

    assert cluster_of(db.conn, p1) != cluster_of(db.conn, p2)


# ---- окно и границы ----

def test_window_48h_is_respected(db):
    a, b = source(db, "example_channel_5"), source(db, "verstov_info", "vk")
    text = "В Магнитогорске сгорел склад на улице Ленина, пострадавших нет."
    p1 = post(db, a, text, at="2026-09-01T10:00:00+00:00")
    p2 = post(db, b, text, source_type="vk", at="2026-09-03T11:00:00+00:00")  # +49 ч

    run(db)

    assert cluster_of(db.conn, p1) != cluster_of(db.conn, p2)


def test_window_is_a_config_parameter(db):
    a, b = source(db, "example_channel_5"), source(db, "verstov_info", "vk")
    text = "В Магнитогорске сгорел склад на улице Ленина, пострадавших нет."
    p1 = post(db, a, text, at="2026-09-01T10:00:00+00:00")
    p2 = post(db, b, text, source_type="vk", at="2026-09-03T11:00:00+00:00")

    run(db, window_hours=72)

    assert cluster_of(db.conn, p1) == cluster_of(db.conn, p2)


def test_cities_never_mix(db):
    """Одна и та же формулировка в двух городах — две новости для двух каналов."""
    a = source(db, "example_channel_5", city="magnitogorsk")
    b = source(db, "chp_nvkz", city="novokuznetsk")
    text = "На проспекте Металлургов столкнулись два автомобиля, есть пострадавшие."
    p1 = post(db, a, text)
    p2 = post(db, b, text)

    dedup, _ = run(db)
    dedup.process_feed(feed(db, city="novokuznetsk"))

    assert cluster_of(db.conn, p1) != cluster_of(db.conn, p2)


# ---- кросс-платформенность (критерий приёмки фазы 3) ----

def test_one_news_from_three_platforms_is_one_cluster(db):
    tg = source(db, "example_channel_5", "tg")
    vk = source(db, "verstov_info", "vk")
    rss = source(db, "https://mgorsk.ru/rss", "rss")
    text = "В Магнитогорске на проспекте Ленина столкнулись автобус и легковой автомобиль."
    p1 = post(db, tg, text, at="2026-09-01T10:00:00+00:00")
    p2 = post(db, vk, text + "\n\nПодписывайтесь", source_type="vk", at="2026-09-01T10:12:00+00:00")
    p3 = post(db, rss, "На проспекте Ленина автобус столкнулся с легковушкой, пострадали двое",
              source_type="rss", at="2026-09-01T10:30:00+00:00")
    emb = FakeEmbedder({"столкнулись автобус": 0.0, "автобус столкнулся": 15.0})

    run(db, emb)

    cid = cluster_of(db.conn, p1)
    assert cluster_of(db.conn, p2) == cid and cluster_of(db.conn, p3) == cid
    assert len(cluster_members(db.conn, cid)) == 3
    row = db.conn.execute("SELECT size FROM clusters WHERE id=?", (cid,)).fetchone()
    assert row["size"] == 3


# ---- уровень 1: картинки ----

def test_same_photo_merges_posts(db, tmp_path):
    from PIL import Image
    img = Image.effect_noise((256, 256), 60).convert("RGB").resize((256, 256))
    p_a, p_b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    img.save(p_a, quality=92)
    img.save(p_b, quality=40)

    a, b = source(db, "example_channel_5"), source(db, "verstov_info", "vk")
    p1 = post(db, a, "Утром на Ленина произошло серьёзное ДТП с автобусом", media_path=str(p_a))
    p2 = post(db, b, "ДТП с автобусом на Ленина: подробности от очевидцев",
              source_type="vk", at="2026-09-01T10:30:00+00:00", media_path=str(p_b))
    emb = FakeEmbedder({"Утром на Ленина": 0.0, "ДТП с автобусом на Ленина": 40.0})  # cos 0.77

    _, decisions = run(db, emb)

    assert cluster_of(db.conn, p1) == cluster_of(db.conn, p2)
    assert decisions[1].reason == "phash"


def test_source_template_image_does_not_merge(db, tmp_path):
    """Шапка рубрики приходит с каждым постом паблика — склеивать по ней нельзя."""
    from PIL import Image
    logo = tmp_path / "logo.jpg"
    Image.effect_noise((256, 256), 60).convert("RGB").save(logo, quality=95)

    a = source(db, "mgorsk_news")
    ids = [
        post(db, a, f"Новость номер {i} про совершенно разные события в городе",
             at=f"2026-09-01T1{i}:00:00+00:00", media_path=str(logo))
        for i in range(4)
    ]

    run(db)

    assert len({cluster_of(db.conn, pid) for pid in ids}) == 4


def test_photo_merge_requires_texts_not_to_contradict(db, tmp_path):
    """Одна картинка при совсем разных текстах — повод усомниться, а не склеить."""
    from PIL import Image
    img = Image.effect_noise((256, 256), 60).convert("RGB")
    p_a, p_b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    img.save(p_a, quality=92)
    img.save(p_b, quality=90)

    a, b = source(db, "example_channel_5"), source(db, "verstov_info", "vk")
    p1 = post(db, a, "Крупное ДТП с автобусом на проспекте Ленина сегодня утром",
              media_path=str(p_a))
    p2 = post(db, b, "Городская администрация утвердила план ремонта теплотрасс",
              source_type="vk", at="2026-09-01T10:30:00+00:00", media_path=str(p_b))

    run(db)   # тексты незнакомые фейку → ортогональные векторы, cos = 0

    assert cluster_of(db.conn, p1) != cluster_of(db.conn, p2)


# ---- альбомы ----

def test_album_counts_as_one_member(db):
    a = source(db, "example_channel_5")
    head = post(db, a, "Пожар на складе: фоторепортаж с места", grouped_id="777",
                external_id="100")
    tail = post(db, a, "", grouped_id="777", external_id="101")

    _, decisions = run(db)

    cid = cluster_of(db.conn, head)
    assert cid is not None
    assert cluster_of(db.conn, tail) is None      # хвост альбома отдельным постом не считается
    assert len(cluster_members(db.conn, cid)) == 1
    assert len(decisions) == 1


# ---- цепочки ----

def test_chaining_is_stopped_by_centroid_check(db):
    """A~B и B~C, но C далеко от A: без проверки по кластеру C утащил бы
    в одну новость два разных сюжета."""
    a, b, c = source(db, "s1"), source(db, "s2", "vk"), source(db, "s3", "rss")
    p1 = post(db, a, "Первая новость дня про происшествие на левом берегу города")
    p2 = post(db, b, "Вторая формулировка того же происшествия на левом берегу",
              source_type="vk", at="2026-09-01T10:20:00+00:00")
    p3 = post(db, c, "Третья новость, к первой отношения уже не имеющая совсем",
              source_type="rss", at="2026-09-01T10:40:00+00:00")
    emb = FakeEmbedder({"Первая новость": 0.0, "Вторая формулировка": 25.0,
                        "Третья новость": 50.0})   # cos: A~B .91, B~C .91, A~C .64

    run(db, emb)

    assert cluster_of(db.conn, p1) == cluster_of(db.conn, p2)
    assert cluster_of(db.conn, p3) != cluster_of(db.conn, p1)


# ---- инкрементальность ----

def test_new_post_does_not_recompute_the_feed(db):
    a = source(db, "example_channel_5")
    for i in range(5):
        post(db, a, f"Новость дня номер {i} о происшествиях в городе Магнитогорске",
             at=f"2026-09-01T1{i}:00:00+00:00")
    emb = FakeEmbedder()
    dedup, _ = run(db, emb)
    encoded_before = len(emb.encoded)

    post(db, a, "Свежая новость о происшествии на проспекте Карла Маркса",
         at="2026-09-01T16:00:00+00:00")
    decisions = dedup.process_feed(feed(db))

    assert len(decisions) == 1                       # обработан только новый пост
    assert len(emb.encoded) == encoded_before + 1    # и закодирован тоже только он


def test_rerun_over_same_feed_changes_nothing(db):
    a, b = source(db, "example_channel_5"), source(db, "verstov_info", "vk")
    text = "В Магнитогорске сгорел склад на улице Ленина, пострадавших нет."
    post(db, a, text)
    post(db, b, text, source_type="vk", at="2026-09-01T10:20:00+00:00")

    dedup, first = run(db)
    second = dedup.process_feed(feed(db))

    assert len(first) == 2 and second == []
    assert db.conn.execute("SELECT COUNT(*) c FROM clusters").fetchone()["c"] == 1
    assert db.conn.execute("SELECT COUNT(*) c FROM cluster_members").fetchone()["c"] == 2


# ---- правило «драфт уже есть» (спека §3) ----

def test_post_joining_cluster_with_draft_does_not_spawn_publication(db):
    a, b = source(db, "example_channel_5"), source(db, "verstov_info", "vk")
    text = "В Магнитогорске сгорел склад на улице Ленина, пострадавших нет."
    p1 = post(db, a, text)
    dedup, first = run(db)
    assert first[0].should_create_draft is True

    cid = cluster_of(db.conn, p1)
    db.conn.execute(
        "INSERT INTO drafts (cluster_id, city, title, status) VALUES (?,?,?,'published')",
        (cid, CITY, "Сгорел склад"))

    p2 = post(db, b, text, source_type="vk", at="2026-09-01T10:20:00+00:00")
    [decision] = dedup.process_feed(feed(db))

    assert decision.cluster_id == cid          # фактура добавлена в кластер
    assert cluster_of(db.conn, p2) == cid
    assert decision.should_create_draft is False
    assert decision.draft_status == "published"


def test_new_cluster_always_asks_for_a_draft(db):
    a = source(db, "example_channel_5")
    post(db, a, "Совершенно новая новость про коммунальную аварию на Труда")

    _, [decision] = run(db)

    assert decision.is_new_cluster and decision.should_create_draft is True
    assert decision.draft_status is None


# ---- медиа, которое ещё качается ----

def test_media_only_post_waits_for_its_photo(db, tmp_path):
    a = source(db, "example_channel_5")
    now = "2026-09-01T10:05:00+00:00"
    post(db, a, "", at="2026-09-01T10:00:00+00:00", media_path=None)
    db.conn.execute("UPDATE raw_posts SET has_media=1, media_state='pending'")
    pid = db.conn.execute("SELECT id FROM raw_posts").fetchone()["id"]
    db.add_media(pid, 0, "photo")

    dedup = Deduplicator(db, FakeEmbedder(), DedupSettings(), now=now)
    decisions = dedup.process_feed(feed(db))

    assert decisions == []                       # ждём картинку
    assert cluster_of(db.conn, pid) is None


def test_media_only_post_is_clustered_once_photo_arrives(db, tmp_path):
    from PIL import Image
    path = tmp_path / "a.jpg"
    Image.effect_noise((256, 256), 60).convert("RGB").save(path)

    a = source(db, "example_channel_5")
    pid = post(db, a, "", media_path=str(path))

    dedup = Deduplicator(db, FakeEmbedder(), DedupSettings(),
                         now="2026-09-01T10:05:00+00:00")
    decisions = dedup.process_feed(feed(db))

    assert len(decisions) == 1 and cluster_of(db.conn, pid) is not None


def test_media_only_post_is_not_deferred_forever(db):
    """Картинка не пришла за 15 минут — пост всё равно идёт в ленту как есть."""
    a = source(db, "example_channel_5")
    pid = post(db, a, "", at="2026-09-01T10:00:00+00:00")
    db.conn.execute("UPDATE raw_posts SET has_media=1, media_state='pending'")
    db.add_media(pid, 0, "photo")

    dedup = Deduplicator(db, FakeEmbedder(), DedupSettings(),
                         now="2026-09-01T11:00:00+00:00")
    decisions = dedup.process_feed(feed(db))

    assert len(decisions) == 1 and cluster_of(db.conn, pid) is not None


def test_cross_source_template_image_does_not_merge(db, tmp_path):
    """Стоковая заставка, которую переиспользует источник, не склеивает чужие посты."""
    from PIL import Image
    logo = tmp_path / "logo.jpg"
    Image.effect_noise((256, 256), 60).convert("RGB").save(logo, quality=95)

    a, b = source(db, "mgorsk_news"), source(db, "verstov_info", "vk")
    for i in range(3):
        post(db, a, f"Дежурная сводка происшествий за {i} сентября в Магнитогорске",
             at=f"2026-09-01T1{i}:00:00+00:00", media_path=str(logo))
    outsider = post(db, b, "Совсем другая новость про ремонт теплотрассы на Труда",
                    source_type="vk", at="2026-09-01T15:00:00+00:00", media_path=str(logo))

    run(db)

    assert cluster_of(db.conn, outsider) not in {
        cluster_of(db.conn, r["id"])
        for r in db.conn.execute("SELECT id FROM raw_posts WHERE source_id=?", (a,))
    }


# ---- регулярные рубрики источника ----

def test_daily_rubric_from_one_source_does_not_merge(db):
    """«Погода на 1 сентября» и «Погода на 2 сентября» — две рубрики, а не одна
    новость. Признак: тот же источник и разрыв ровно в сутки (живая лента)."""
    a = source(db, "mrinfo74")
    p1 = post(db, a, "Доброе утро! Погода на 1 сентября: минимальная температура 10, максимальная 22",
              at="2026-09-01T02:00:00+00:00")
    p2 = post(db, a, "Доброе утро! Погода на 2 сентября: минимальная температура 11, максимальная 23",
              at="2026-09-02T02:05:00+00:00")
    emb = FakeEmbedder({"Погода на 1 сентября": 0.0, "Погода на 2 сентября": 5.0})  # cos 0.996

    run(db, emb)

    assert cluster_of(db.conn, p1) != cluster_of(db.conn, p2)


def test_two_sources_within_the_day_merge(db):
    """Тот же сюжет у ДВУХ источников внутри суток — обычная склейка.
    (Внутри одного источника семантика выключена, см. тест про «Момент ДТП».)"""
    a, b = source(db, "mrinfo74"), source(db, "verstov_info", "vk")
    p1 = post(db, a, "На проспекте Ленина столкнулись автобус и легковой автомобиль",
              at="2026-09-01T10:00:00+00:00")
    p2 = post(db, b, "ДТП на Ленина: автобус и легковушка, подробности от очевидцев",
              source_type="vk", at="2026-09-01T15:00:00+00:00")
    emb = FakeEmbedder({"столкнулись автобус": 0.0, "ДТП на Ленина": 15.0})

    run(db, emb)

    assert cluster_of(db.conn, p1) == cluster_of(db.conn, p2)


def test_identical_text_from_one_source_a_day_later_is_a_rubric(db):
    """Тот же текст того же источника ровно через сутки — рубрика, а не дубль."""
    a = source(db, "magnitogorsk_news")
    text = "Спокойной ночи, магнитогорцы. Городские новости вернутся завтра утром."
    p1 = post(db, a, text, at="2026-09-01T17:00:00+00:00")
    p2 = post(db, a, text, at="2026-09-02T17:00:00+00:00")

    run(db)

    assert cluster_of(db.conn, p1) != cluster_of(db.conn, p2)


def test_two_sources_a_day_apart_still_merge(db):
    """Предохранитель рубрик касается только своего источника: перепечатка
    чужой новости через сутки — обычная склейка."""
    a, b = source(db, "example_channel_5"), source(db, "verstov_info", "vk")
    text = "В Магнитогорске сгорел склад на улице Ленина, пострадавших нет."
    p1 = post(db, a, text, at="2026-09-01T17:00:00+00:00")
    p2 = post(db, b, text, source_type="vk", at="2026-09-02T17:00:00+00:00")

    run(db)

    assert cluster_of(db.conn, p1) == cluster_of(db.conn, p2)


def test_same_template_different_names_do_not_merge(db):
    """Две ориентировки на розыск разных подростков написаны почти одинаково
    (cosine 0.87 на живой ленте), но имена и города в них разные — это две
    новости, а не одна."""
    a, b = source(db, "kuz_plus"), source(db, "news10kanal", "vk")
    p1 = post(db, a, "В Междуреченске разыскивают 14-летнего Александра Кудреватова. "
                     "Он ушёл из дома по улице Гули Королевой и не вернулся. "
                     "Приметы: рост 175 см. Полицейские просят помощи.")
    p2 = post(db, b, "Розыск подростка. В отдел полиции Рудничный по городу Прокопьевску "
                     "обратился житель: его сын Захар Кузнецов ушёл из дома по улице "
                     "2-я Березовская. Приметы: рост 165 см. Полицейские просят помощи.",
              source_type="vk", at="2026-09-01T13:00:00+00:00")
    emb = FakeEmbedder({"Кудреватова": 0.0, "Кузнецов": 29.0})   # cos 0.87

    run(db, emb)

    assert cluster_of(db.conn, p1) != cluster_of(db.conn, p2)


def test_same_event_told_differently_still_merges(db):
    """Предохранитель по именам не мешает: у одной новости имена совпадают."""
    a, b = source(db, "kuz_plus"), source(db, "news10kanal", "vk")
    p1 = post(db, a, "В Междуреченске разыскивают 14-летнего Александра Кудреватова, "
                     "он ушёл из дома по улице Гули Королевой и не вернулся.")
    p2 = post(db, b, "Пропавшего в Междуреченске школьника Александра Кудреватова нашли "
                     "живым, сообщили в полиции Кузбасса.",
              source_type="vk", at="2026-09-01T13:00:00+00:00")
    emb = FakeEmbedder({"разыскивают": 0.0, "нашли": 29.0})

    run(db, emb)

    assert cluster_of(db.conn, p1) == cluster_of(db.conn, p2)


def test_near_copy_merges_even_without_shared_names(db):
    """Почти дословное совпадение (0.97+) склеиваем и без общих имён:
    проверка имён — предохранитель для середины шкалы, а не для копипаста."""
    a, b = source(db, "example_channel_5"), source(db, "verstov_info", "vk")
    p1 = post(db, a, "Смертельное ДТП произошло вечером на трассе у поворота на Агаповку")
    p2 = post(db, b, "Вечером на трассе у поворота на Агаповку случилось смертельное ДТП",
              source_type="vk", at="2026-09-01T10:30:00+00:00")
    emb = FakeEmbedder({"Смертельное ДТП": 0.0, "случилось смертельное": 12.0})  # cos 0.978

    run(db, emb)

    assert cluster_of(db.conn, p1) == cluster_of(db.conn, p2)


def test_semantic_merge_inside_one_source_is_off_by_default(db):
    """Канал ДТП публикует «03.08. Момент ДТП на кольце …» по десять раз в день:
    для модели это один и тот же текст (0.96), а на деле — разные аварии.
    Ценность кластера — в РАЗНЫХ источниках, поэтому семантику внутри источника
    выключаем; точный хэш и картинка внутри источника остаются."""
    a = source(db, "dtp_nk")
    p1 = post(db, a, "03.08. Момент ДТП на кольце Павловского-Запорожская в Новокузнецке",
              at="2026-09-01T10:00:00+00:00")
    p2 = post(db, a, "03.08. Момент ДТП на кольце Транспортная-Циолковского в Новокузнецке",
              at="2026-09-01T14:00:00+00:00")
    emb = FakeEmbedder({"Павловского": 0.0, "Транспортная": 10.0})   # cos 0.98

    run(db, emb)

    assert cluster_of(db.conn, p1) != cluster_of(db.conn, p2)


def test_exact_repost_inside_one_source_still_merges(db):
    """Тот же текст, выложенный источником повторно через час, — это дубль."""
    a = source(db, "gorod_cherepovets")
    text = "Водители, будьте внимательны у лесополосы: в наших краях снова видели лосей."
    p1 = post(db, a, text, at="2026-09-01T10:40:00+00:00")
    p2 = post(db, a, text, at="2026-09-01T12:08:00+00:00")

    run(db)

    assert cluster_of(db.conn, p1) == cluster_of(db.conn, p2)


def test_same_source_semantic_can_be_enabled_in_config(db):
    a = source(db, "mrinfo74")
    p1 = post(db, a, "Школа №42 в посёлке Димитрова приняла первых учеников после стройки",
              at="2026-09-01T10:00:00+00:00")
    p2 = post(db, a, "В Магнитогорске открыли новое здание школы № 42 в Димитрова",
              at="2026-09-01T16:00:00+00:00")
    emb = FakeEmbedder({"приняла первых": 0.0, "открыли новое": 20.0})

    run(db, emb, same_source_semantic=True)

    assert cluster_of(db.conn, p1) == cluster_of(db.conn, p2)


# ---- повторяющиеся объявления и срок жизни кластера ----

def test_repeated_announcement_merges_only_inside_its_episode(db):
    """Объявление «введён режим Беспилотная опасность» приходит слово в слово
    каждый раз, когда режим вводят. Внутри одного эпизода источники дублируют
    друг друга (это склейка), но вчерашняя тревога — другая новость."""
    a, b = source(db, "example_channel_6", city="cherepovets"), \
        source(db, "cherprachka", city="cherepovets")
    text = ("По информации Минобороны России, на территории Вологодской области "
            "отменён режим «Беспилотная опасность». Работают оперативные службы.")
    day1_a = post(db, a, text, at="2026-08-21T06:33:00+00:00")
    day1_b = post(db, b, text, at="2026-08-21T06:35:00+00:00")
    day2_a = post(db, a, text, at="2026-08-22T15:24:00+00:00")
    day2_b = post(db, b, text, at="2026-08-22T15:30:00+00:00")

    dedup = Deduplicator(db, FakeEmbedder(), DedupSettings())
    dedup.process_feed(list(reversed(normalized_feed(db, city="cherepovets", limit=500))))

    assert cluster_of(db.conn, day1_a) == cluster_of(db.conn, day1_b)   # один эпизод
    assert cluster_of(db.conn, day2_a) == cluster_of(db.conn, day2_b)
    assert cluster_of(db.conn, day1_a) != cluster_of(db.conn, day2_a)   # разные эпизоды


def test_unique_text_is_not_treated_as_a_template(db):
    """Обычная новость, перепечатанная через сутки, склеивается по-прежнему."""
    a, b = source(db, "example_channel_5"), source(db, "verstov_info", "vk")
    text = "В Магнитогорске сгорел склад на улице Ленина, пострадавших нет."
    p1 = post(db, a, text, at="2026-09-01T10:00:00+00:00")
    p2 = post(db, b, text, source_type="vk", at="2026-09-02T04:00:00+00:00")

    run(db)

    assert cluster_of(db.conn, p1) == cluster_of(db.conn, p2)


def test_cluster_never_spans_more_than_the_window(db):
    """Кластер живёт окно и не растягивается цепочкой: пост, пришедший через
    48 ч после первого, начинает новый кластер, даже если похож на средний."""
    a, b, c = source(db, "s1"), source(db, "s2", "vk"), source(db, "s3", "rss")
    text = "В Магнитогорске обсуждают ремонт трамвайных путей на улице Гагарина сегодня"
    p1 = post(db, a, text, at="2026-09-01T00:00:00+00:00")
    p2 = post(db, b, text, source_type="vk", at="2026-09-02T20:00:00+00:00")
    p3 = post(db, c, text, source_type="rss", at="2026-09-04T10:00:00+00:00")

    run(db)

    assert cluster_of(db.conn, p1) == cluster_of(db.conn, p2)
    assert cluster_of(db.conn, p3) != cluster_of(db.conn, p1)
