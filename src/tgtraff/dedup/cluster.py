"""Кластеризация: два уровня дедупа поверх нормализованной ленты (спека §3).

Один пост — одно решение:

    уровень 1  точный хэш нормализованного текста   → та же новость наверняка
    уровень 1  perceptual hash картинки (межисточниковый) → та же новость
    уровень 2  cosine эмбеддингов > порога в окне 48 ч    → та же новость
    иначе                                                 → новый кластер

Инкрементальность: считаем подпись НОВОГО поста и сравниваем её с окном уже
посчитанных подписей своего города. Никаких пересчётов ленты, стоимость поста —
одно кодирование текста и одно умножение матриц.

Цепочки (A~B, B~C, но A≁C) рвём проверкой по кластеру целиком: присоединение
требует не только близкого соседа, но и среднего сходства с кластером не ниже
`threshold - centroid_margin`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from tgtraff.dedup import store
from tgtraff.dedup.phash import hamming, phash_file
from tgtraff.dedup.settings import DedupSettings
from tgtraff.dedup.text import embed_text, entity_keys, normalize_text, text_fingerprint
from tgtraff.normalize import NormalizedItem

log = logging.getLogger("tgtraff.dedup")

REASON_NEW = "new"
REASON_EXACT = "exact_text"
REASON_PHASH = "phash"
REASON_SEMANTIC = "semantic"


@dataclass(frozen=True)
class Decision:
    """Что случилось с постом и нужна ли по нему публикация."""

    raw_post_id: int
    cluster_id: int
    is_new_cluster: bool
    reason: str
    similarity: float | None = None
    matched_raw_post_id: int | None = None
    # Спека §3: пост, попавший в кластер с уже заведённым драфтом, публикацию
    # не порождает — фактура добавляется в кластер, и на этом всё.
    should_create_draft: bool = True
    draft_status: str | None = None


class Deduplicator:
    def __init__(self, db, embedder, settings: DedupSettings | None = None,
                 now: str | datetime | None = None):
        self.db = db
        self.conn = db.conn
        self.embedder = embedder
        self.settings = settings or DedupSettings()
        self._now = now

    # ---------- время ----------

    def now(self) -> datetime:
        if self._now is None:
            return datetime.now(timezone.utc)
        return store.parse_ts(self._now) if isinstance(self._now, str) else self._now

    # ---------- вход ----------

    def process_feed(self, items: list[NormalizedItem]) -> list[Decision]:
        """Лента в хронологическом порядке → решения по новым постам.

        Порядок важен: кластер должен начинаться с самого раннего поста новости,
        иначе «первоисточник» окажется присоединённым к перепечатке."""
        decisions = []
        for item in items:
            decision = self.process(item)
            if decision is not None:
                decisions.append(decision)
        return decisions

    def process(self, item: NormalizedItem) -> Decision | None:
        """Один логический пост. None — пост пропущен (уже в кластере или ждёт медиа)."""
        head_id = item.head_id
        if store.cluster_of(self.conn, head_id) is not None:
            return None
        if self._defer(item):
            return None

        self._store_media_hashes(item)

        body = embed_text(item.text)
        text_hash = text_fingerprint(item.text)
        text_chars = len(normalize_text(item.text))
        entities = entity_keys(item.text)
        vector = self._encode(body)

        store.save_signature(
            self.conn, head_id, city=item.city, source_id=item.source_id,
            published_at=item.published_at, text_hash=text_hash, text_chars=text_chars,
            embedding=vector, embed_model=getattr(self.embedder, "name", None),
            entities=entities,
        )

        window = store.window_signatures(
            self.conn, city=item.city, at=item.published_at,
            hours=self.settings.window_hours,
            model=getattr(self.embedder, "name", None), exclude_post_id=head_id,
        )
        own = set(item.raw_post_ids)
        window = [s for s in window
                  if s.raw_post_id not in own and not self._is_recurring(item, s)]

        # Повторяющееся объявление склеиваем только с соседями по эпизоду.
        if self._is_repeating_template(item, text_hash):
            gap = timedelta(hours=self.settings.template_max_gap_hours)
            at = store.parse_ts(item.published_at)
            window = [s for s in window
                      if abs(store.parse_ts(s.published_at) - at) <= gap]

        match = (self._match_exact(item, text_hash, window)
                 or self._match_phash(item, vector, window)
                 or self._match_semantic(item, vector, text_chars, entities, window))

        if match is None:
            cluster_id = store.create_cluster(self.conn, item.city, head_id, text_hash,
                                              item.published_at)
            store.add_member(self.conn, cluster_id, head_id, None, REASON_NEW, None,
                             item.published_at)
            log.debug("новый кластер %s: %s", cluster_id, body[:60])
            return Decision(raw_post_id=head_id, cluster_id=cluster_id, is_new_cluster=True,
                            reason=REASON_NEW, should_create_draft=True)

        cluster_id, similarity, reason, matched_id = match
        store.add_member(self.conn, cluster_id, head_id, similarity, reason, matched_id,
                         item.published_at)
        draft = store.cluster_draft(self.conn, cluster_id)
        log.debug("пост %s → кластер %s (%s, %.3f)", head_id, cluster_id, reason,
                  similarity or 0.0)
        return Decision(
            raw_post_id=head_id, cluster_id=cluster_id, is_new_cluster=False, reason=reason,
            similarity=similarity, matched_raw_post_id=matched_id,
            should_create_draft=False, draft_status=draft["status"] if draft else None,
        )

    # ---------- подготовка поста ----------

    def _defer(self, item: NormalizedItem) -> bool:
        """Пост без текста, у которого ещё качается картинка, кластеризовать нечем.

        Ждём — но не бесконечно: если медиа не пришло за `defer_minutes`, пост
        идёт в ленту как есть, иначе он потеряется совсем."""
        if item.text.strip() or item.media_ready or not item.media:
            return False
        age = self.now() - store.parse_ts(item.published_at)
        return age < timedelta(minutes=self.settings.defer_minutes)

    def _is_recurring(self, item: NormalizedItem, sig: store.Signature) -> bool:
        """Кандидат — не новость, а очередной выпуск рубрики того же источника.

        «Погода на 1 сентября» и «Погода на 2 сентября» похожи почти на единицу
        при любом пороге; отличает их не текст, а ритм: тот же источник и разрыв
        почти ровно в сутки. Чужие источники правило не касается — перепечатка
        новости через день склеивается как обычно."""
        if not self.settings.same_source_daily_guard or sig.source_id != item.source_id:
            return False
        gap = abs((store.parse_ts(item.published_at)
                   - store.parse_ts(sig.published_at)).total_seconds())
        days = round(gap / 86400)
        tolerance = self.settings.daily_gap_tolerance_min * 60
        return days >= 1 and abs(gap - days * 86400) <= tolerance

    def _is_repeating_template(self, item: NormalizedItem, text_hash: str | None) -> bool:
        if not text_hash or self.settings.template_min_repeats <= 0:
            return False
        repeats = store.text_hash_repeats(self.conn, item.city, text_hash)
        return repeats >= self.settings.template_min_repeats

    def _cluster_in_window(self, cluster_id: int, at: str) -> bool:
        """Кластер живёт окно: пост, отстоящий от начала кластера дальше окна,
        начинает новый — иначе цепочка «каждый следующий в пределах 48 ч»
        растягивает один кластер на неделю."""
        first, last = store.cluster_bounds(self.conn, cluster_id)
        if not first:
            return True
        moment = store.parse_ts(at)
        limit = timedelta(hours=self.settings.window_hours)
        return (abs(moment - store.parse_ts(first)) <= limit
                and abs(moment - store.parse_ts(last)) <= limit)

    def _store_media_hashes(self, item: NormalizedItem) -> None:
        for media_id, path in store.post_media_to_hash(self.conn, item.raw_post_ids):
            store.save_phash(self.conn, media_id, phash_file(path))

    def _encode(self, body: str) -> np.ndarray | None:
        if not body:
            return None
        return self.embedder.encode([body])[0]

    # ---------- уровень 1 ----------

    def _match_exact(self, item: NormalizedItem, text_hash: str | None,
                     window: list[store.Signature]):
        if not text_hash:
            return None
        for sig in window:
            if (sig.text_hash == text_hash and sig.cluster_id
                    and self._cluster_in_window(sig.cluster_id, item.published_at)):
                return sig.cluster_id, 1.0, REASON_EXACT, sig.raw_post_id
        return None

    def _match_phash(self, item: NormalizedItem, vector, window: list[store.Signature]):
        """Одна и та же фотография у двух РАЗНЫХ источников — одна новость.

        Внутри источника по картинке не склеиваем никогда: шапка рубрики,
        логотип и заставка приходят с каждым постом паблика."""
        mine = store.post_phashes(self.conn, item.raw_post_ids)
        if not mine:
            return None

        by_post = {s.raw_post_id: s for s in window}
        candidates = store.window_phashes(self.conn, city=item.city, at=item.published_at,
                                          hours=self.settings.window_hours)
        for cand in candidates:
            if cand.raw_post_id in set(item.raw_post_ids) or cand.source_id == item.source_id:
                continue
            if not cand.cluster_id:
                continue
            if not self._cluster_in_window(cand.cluster_id, item.published_at):
                continue
            if min((hamming(ph, cand.phash) for ph in mine), default=64) > self.settings.phash_max_distance:
                continue
            if self._is_boilerplate(cand.phash, cand.source_id, item.source_id):
                continue
            sig = by_post.get(cand.raw_post_id)
            similarity = self._cosine(vector, sig.embedding if sig else None)
            # Совсем разные тексты при одной картинке — повод усомниться:
            # это чаще стоковая иллюстрация, чем одна новость.
            if similarity is not None and similarity < self.settings.phash_text_floor:
                continue
            return cand.cluster_id, similarity, REASON_PHASH, cand.raw_post_id
        return None

    def _is_boilerplate(self, phash: str, *source_ids: int) -> bool:
        limit = self.settings.phash_boilerplate_min
        return any(store.phash_usage_by_source(self.conn, phash, sid) >= limit
                   for sid in set(source_ids))

    # ---------- уровень 2 ----------

    def _match_semantic(self, item: NormalizedItem, vector, text_chars: int,
                        entities: set[str], window: list[store.Signature]):
        if vector is None or text_chars < self.settings.min_chars_for_semantic:
            return None

        usable = [s for s in window
                  if s.embedding is not None and s.cluster_id
                  and s.text_chars >= self.settings.min_chars_for_semantic
                  and s.embedding.shape == vector.shape
                  and (self.settings.same_source_semantic or s.source_id != item.source_id)]
        if not usable:
            return None

        matrix = np.vstack([s.embedding for s in usable])
        sims = matrix @ np.asarray(vector, dtype=np.float32)

        by_post = {s.raw_post_id: s for s in usable}
        order = np.argsort(-sims)
        rejected: set[int] = set()
        for idx in order:
            score = float(sims[idx])
            if score < self.settings.threshold:
                break
            sig = usable[idx]
            if sig.cluster_id in rejected:
                continue
            if not self._entities_agree(entities, sig.entities, score):
                continue
            if not self._cluster_in_window(sig.cluster_id, item.published_at):
                rejected.add(sig.cluster_id)
                continue
            if not self._fits_cluster(vector, sig.cluster_id, by_post):
                rejected.add(sig.cluster_id)
                continue
            return sig.cluster_id, score, REASON_SEMANTIC, sig.raw_post_id
        return None

    def _entities_agree(self, mine: set[str], theirs: frozenset[str], score: float) -> bool:
        """Хотя бы одно общее имя собственное — иначе это разные новости
        по одному шаблону (две ориентировки на розыск, два пресс-релиза).

        Правило работает только в середине шкалы: почти дословный текст
        (score ≥ entity_guard_max_sim) — это перепечатка, там имён может
        не быть вовсе."""
        s = self.settings
        if not s.entity_guard or score >= s.entity_guard_max_sim:
            return True
        if len(mine) < s.entity_guard_min or len(theirs) < s.entity_guard_min:
            return True
        return bool(mine & theirs)

    def _fits_cluster(self, vector, cluster_id: int, by_post: dict) -> bool:
        """Похож ли пост на кластер целиком — предохранитель от цепочек A~B~C.

        Сравниваем с центроидом (средним вектором участников), а не со средним
        косинусов: у живой новости кластер собирает разные ракурсы одного сюжета
        («пропал» и «нашёлся»), и среднее косинусов рвёт такие кластеры зря,
        а центроид — направление сюжета — держит."""
        members = store.cluster_members(self.conn, cluster_id)
        vectors = [by_post[pid].embedding for pid in members
                   if pid in by_post and by_post[pid].embedding is not None]
        vectors = [v for v in vectors if v.shape == vector.shape]
        if not vectors:
            return True
        centroid = np.mean(np.vstack(vectors), axis=0)
        norm = float(np.linalg.norm(centroid))
        if norm == 0:
            return True
        return float(np.dot(vector, centroid / norm)) >= self.settings.centroid_threshold

    @staticmethod
    def _cosine(a, b) -> float | None:
        if a is None or b is None or getattr(a, "shape", None) != getattr(b, "shape", None):
            return None
        return float(np.dot(a, b))
