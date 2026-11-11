"""Материал кластера — вход rewriter'а (спека §5: «Вход: весь кластер»).

Здесь решается главный вопрос фазы: что считать фактурой. Плейсхолдер
«В посте есть вложения:» фактурой не является (решение ревью 2026-09-03),
пост без текста рерайтится по медиа и фактуре остальных постов кластера.
"""
import pytest

from tgtraff.config import SourceSpec
from tgtraff.db import Database
from tgtraff.rewriter.material import collect_material, is_placeholder

CITY = "magnitogorsk"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def source(db, key, source_type="tg", official=False):
    return db.upsert_source(SourceSpec(city=CITY, source_type=source_type, key=key,
                                       official=official))


def post(db, source_id, text, at="2026-09-01T10:00:00+00:00", source_type="tg",
         media_path=None, url=None):
    res = db.insert_raw_post(source_id=source_id, source_type=source_type,
                             external_id=str(abs(hash((source_id, text, at))) % 10**7),
                             published_at=at, text=text, url=url,
                             has_media=1 if media_path else 0,
                             media_state="pending" if media_path else "none")
    if media_path:
        media_id = db.add_media(res.id, 0, "photo")
        db.conn.execute("UPDATE raw_media SET status='done', file_path=? WHERE id=?",
                        (media_path, media_id))
    return res.id


def cluster(db, *post_ids, city=CITY):
    cur = db.conn.execute(
        "INSERT INTO clusters (city, status, lead_raw_post_id, size, first_seen_at, last_seen_at) "
        "VALUES (?, 'open', ?, ?, '2026-09-01T10:00:00+00:00', '2026-09-01T10:00:00+00:00')",
        (city, post_ids[0], len(post_ids)))
    cid = cur.lastrowid
    for i, pid in enumerate(post_ids):
        db.conn.execute(
            "INSERT INTO cluster_members (cluster_id, raw_post_id, reason) VALUES (?,?,?)",
            (cid, pid, "new" if i == 0 else "semantic"))
    return cid


# --- плейсхолдеры ----------------------------------------------------------

@pytest.mark.parametrize("text", [
    "В посте есть вложения:",
    "(без текста, только медиа)",
    "Фото",
    "Видео",
    "",
    "   ",
])
def test_placeholder_texts_carry_no_facts(text):
    assert is_placeholder(text) is True


def test_real_news_is_not_a_placeholder():
    assert is_placeholder("Горит расселённый дом на Наседкина, эвакуированы 12 человек") is False


# --- сбор материала --------------------------------------------------------

def test_material_collects_every_post_of_the_cluster(db):
    a, b = source(db, "example_channel_13"), source(db, "typical.magnitogorsk", "vk")
    p1 = post(db, a, "На Ленина столкнулись две легковушки, пострадал водитель")
    p2 = post(db, b, "ДТП на Ленина: пострадал один человек", source_type="vk")
    cid = cluster(db, p1, p2)

    m = collect_material(db, cid)

    assert m.cluster_id == cid
    assert [p.source_key for p in m.posts] == ["example_channel_13", "typical.magnitogorsk"]
    assert m.platforms == ["tg", "vk"]


def test_footers_are_stripped_from_the_material(db):
    a = source(db, "example_channel_13")
    p = post(db, a, "Сгорел склад на Ленина, пострадавших нет.\n\nПодписаться на канал")
    m = collect_material(db, cluster(db, p))
    assert "Подписаться" not in m.posts[0].text
    assert "склад" in m.posts[0].text


def test_official_source_is_marked_in_material(db):
    """Медиа-каскад (спека §5) ставит МЧС и ГИБДД на первую ступень."""
    a = source(db, "chelmchs", "vk", official=True)
    p = post(db, a, "Пожар в частном доме потушен, погибших нет", source_type="vk")
    m = collect_material(db, cluster(db, p))
    assert m.posts[0].official is True


def test_placeholder_post_contributes_media_but_not_facts(db):
    a, b = source(db, "example_channel_5"), source(db, "example_channel_13")
    p1 = post(db, a, "В посте есть вложения:", media_path="/tmp/a.jpg")
    p2 = post(db, b, "Момент столкновения на кольце попал на видеорегистратор: "
                     "пострадали двое, оба в больнице")
    m = collect_material(db, cluster(db, p1, p2))

    assert m.posts[0].is_placeholder is True
    assert m.fact_texts == [m.posts[1].text]
    assert len(m.media) == 1


def test_cluster_of_placeholders_only_is_low_facts(db):
    a = source(db, "example_channel_5")
    p = post(db, a, "В посте есть вложения:", media_path="/tmp/a.jpg")
    m = collect_material(db, cluster(db, p))
    assert m.is_low_facts is True


def test_cluster_with_a_real_story_is_not_low_facts(db):
    a = source(db, "example_channel_13")
    p = post(db, a, "На перекрёстке Ленина и Гагарина столкнулись такси и грузовик, "
                    "пострадал водитель такси, его увезли в больницу №3")
    m = collect_material(db, cluster(db, p))
    assert m.is_low_facts is False


def test_short_one_line_post_is_low_facts(db):
    a = source(db, "example_channel_5")
    p = post(db, a, "Авария на Ленина")
    m = collect_material(db, cluster(db, p))
    assert m.is_low_facts is True


# --- промпт ----------------------------------------------------------------

def test_prompt_lists_sources_and_texts(db):
    a, b = source(db, "example_channel_13"), source(db, "chelmchs", "vk", official=True)
    p1 = post(db, a, "Горит склад на Ленина, работают три расчёта")
    p2 = post(db, b, "Пожар на улице Ленина ликвидирован", source_type="vk")
    text = collect_material(db, cluster(db, p1, p2)).as_prompt()

    assert "example_channel_13" in text and "chelmchs" in text
    assert "Горит склад" in text and "ликвидирован" in text
    assert "официальный" in text.lower()


def test_prompt_marks_placeholder_posts_explicitly(db):
    a = source(db, "example_channel_5")
    p1 = post(db, a, "В посте есть вложения:", media_path="/tmp/a.jpg")
    p2 = post(db, source(db, "example_channel_13"), "Столкнулись автобус и легковушка на Труда")
    text = collect_material(db, cluster(db, p1, p2)).as_prompt()

    assert "В посте есть вложения:" not in text
    assert "без текста" in text.lower()


def test_low_facts_threshold_is_configurable(db):
    a = source(db, "news_nvkz", "rss")
    p = post(db, a, "Пропавшую пять дней назад 14-летнюю девушку нашли живой",
             source_type="rss")
    cid = cluster(db, p)
    assert collect_material(db, cid).is_low_facts is True
    assert collect_material(db, cid, min_fact_chars=40).is_low_facts is False
