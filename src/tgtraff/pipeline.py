"""Конвейер фазы 4: кластер → relevance → rewriter → safety → драфт.

    dedup → [relevance → rewriter(LLM) → safety] → очередь модерации
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ это здесь

Порядок узлов — из спеки (§«Архитектура»), и он же экономит деньги: реклама
отсекается правилами до рерайта, а safety смотрит УЖЕ ГОТОВЫЙ текст, потому
что отвечать мы будем за свой пост, а не за формулировки источника.

Автопубликации в фазе 4 нет ни при каком вердикте: все драфты ложатся со
статусом `new` (или `rejected`) и ждут модератора из фазы 5.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from tgtraff import drafts as store
from tgtraff.llm.client import LlmError, LlmUnavailable, NoFacts
from tgtraff.moderation import actions
from tgtraff.relevance.classify import RelevanceChecker
from tgtraff.relevance.settings import RelevanceSettings
from tgtraff.rewriter.cover import render_cover
from tgtraff.rewriter.material import collect_material
from tgtraff.rewriter.media import MediaSettings
from tgtraff.rewriter.rewrite import Rewriter
from tgtraff.rewriter.settings import RewriterSettings
from tgtraff.rewriter.watermark import WatermarkDetector
from tgtraff.safety.checker import SafetyChecker
from tgtraff.safety.rules import REJECT
from tgtraff.safety.settings import SafetySettings

log = logging.getLogger("tgtraff.pipeline")


@dataclass(frozen=True)
class PipelineSettings:
    relevance: RelevanceSettings = field(default_factory=RelevanceSettings)
    rewriter: RewriterSettings = field(default_factory=RewriterSettings)
    safety: SafetySettings = field(default_factory=SafetySettings)
    media: MediaSettings = field(default_factory=MediaSettings)
    vision_model: str | None = None
    # Куда складывать шаблонные обложки ступени 3 каскада.
    cover_dir: str = "data/covers"


@dataclass
class PipelineResult:
    cluster_id: int
    city: str
    status: str                       # draft | dropped | skipped | error
    draft_id: int | None = None
    draft = None
    relevance = None
    safety = None
    cost_usd: float = 0.0
    error: str | None = None


@dataclass
class Stats:
    processed: int = 0
    drafts: int = 0
    dropped: int = 0
    errors: int = 0
    cost_usd: float = 0.0
    by_reason: dict = field(default_factory=dict)

    def add(self, result: PipelineResult) -> None:
        self.cost_usd += result.cost_usd
        if result.status == "skipped":
            return
        self.processed += 1
        if result.status == "draft":
            self.drafts += 1
        elif result.status in ("dropped", "low_facts"):
            # «Писать не из чего» — тоже отсев, только решает его не relevance,
            # а рерайт. В сводке эти кластеры должны быть видны наравне с
            # рекламой и офтопиком, иначе восемь штук за проход исчезают
            # из отчёта вовсе.
            self.dropped += 1
            reason = ("low_facts" if result.status == "low_facts"
                      else result.relevance.verdict if result.relevance else "?")
            self.by_reason[reason] = self.by_reason.get(reason, 0) + 1
        elif result.status == "error":
            self.errors += 1

    def line(self) -> str:
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(self.by_reason.items())) or "—"
        return (f"обработано {self.processed}, драфтов {self.drafts}, "
                f"отсеяно {self.dropped} ({reasons}), ошибок {self.errors}, "
                f"${self.cost_usd:.4f}")


class Phase4Pipeline:
    def __init__(self, db, client, settings: PipelineSettings | None = None,
                 city_names: dict[str, str] | None = None):
        self.db = db
        self.client = client
        # Каждый запрос модели ложится строкой в `llm_usage` с контекстом
        # кластера: без этого «$1.2-2 в день» из спеки нечем проверить.
        self._context: tuple[str | None, int | None] = (None, None)
        self._spent = 0.0
        if hasattr(client, "on_usage"):
            client.on_usage = self._record_usage
        self.settings = settings or PipelineSettings()
        self.city_names = city_names or {}
        self.relevance = RelevanceChecker(client, self.settings.relevance)
        self.rewriter = Rewriter(client, self.settings.rewriter, self.settings.media)
        self.safety = SafetyChecker(client, self.settings.safety)
        self.detector = WatermarkDetector(client, model=self.settings.vision_model)

    # ---------- один кластер ----------

    def process(self, cluster_id: int) -> PipelineResult:
        material = collect_material(
            self.db, cluster_id,
            city_name=self.city_names.get(_city_of(self.db, cluster_id), None),
            min_fact_chars=self.settings.rewriter.min_fact_chars)
        city = material.city
        if store.draft_for_cluster(self.db, cluster_id) is not None or _decided(self.db, cluster_id):
            return PipelineResult(cluster_id, city, "skipped")

        self._context, self._spent = (city, cluster_id), 0.0

        relevance = self.relevance.check(material)
        if relevance.is_drop:
            store.set_cluster_relevance(self.db, cluster_id, relevance.verdict, relevance.reason)
            log.info("кластер #%s отсеян: %s (%s)", cluster_id, relevance.verdict,
                     relevance.reason or relevance.by)
            result = PipelineResult(cluster_id, city, "dropped", cost_usd=self._spent)
            result.relevance = relevance
            return result

        try:
            draft = self.rewriter.rewrite(material, self.detector, db=self.db)
        except NoFacts as exc:
            return self._no_facts(cluster_id, city, relevance, str(exc))
        except LlmError as exc:
            log.warning("кластер #%s: рерайт не удался (%s)", cluster_id, exc)
            result = PipelineResult(cluster_id, city, "error", error=str(exc),
                                    cost_usd=self._spent)
            result.relevance = relevance
            return result

        safety = self.safety.check(draft.text, draft.category)
        media_path = self._media_path(draft)

        draft_id = store.save_draft(
            self.db, cluster_id=cluster_id, city=city, title=draft.title, body=draft.body,
            category=draft.category, urgency=draft.urgency,
            safety_verdict=safety.verdict, safety_reasons=safety.categories,
            flags=draft.flags, relevance=relevance.verdict,
            media_kind=draft.media.kind, media_reason=draft.media.reason,
            media_path=media_path, media_raw_media_id=draft.media.media_id,
            sources=draft.sources, model=draft.model, cost_usd=self._spent,
            notes=_notes(draft, safety), district=draft.district,
            # Автопубликации в фазе 4 нет: даже `safe` ждёт модератора.
            status="rejected" if safety.verdict == REJECT else "new")
        store.set_cluster_relevance(self.db, cluster_id, relevance.verdict, relevance.reason)

        result = PipelineResult(cluster_id, city, "draft", draft_id=draft_id,
                                cost_usd=self._spent)
        result.draft = draft
        result.relevance = relevance
        result.safety = safety
        return result

    def _no_facts(self, cluster_id: int, city: str, relevance, note: str) -> PipelineResult:
        """«Писать не из чего» — решение по кластеру, а не ошибка прохода.

        Записывается тем же способом, что и отклонение модератором с причиной
        «мало фактуры»: тогда кластер и уходит из очереди, и возвращается в неё
        по §28, когда фактура придёт. Своей механики заводить не нужно —
        она уже есть и покрыта тестами."""
        draft_id = store.save_draft(
            self.db, cluster_id=cluster_id, city=city, title="", body="",
            category="other", urgency="normal", relevance=relevance.verdict,
            flags=["low_facts"], notes=note, cost_usd=self._spent, status="new")
        actions.reject(self.db, draft_id, moderator_id=None, reason=actions.LOW_FACTS)
        store.set_cluster_relevance(self.db, cluster_id, relevance.verdict, relevance.reason)
        log.info("кластер #%s: писать не из чего (%s) — вернём по §28, если придёт фактура",
                 cluster_id, note[:120])
        result = PipelineResult(cluster_id, city, "low_facts", draft_id=draft_id,
                                cost_usd=self._spent)
        result.relevance = relevance
        return result

    # ---------- очередь ----------

    def run_once(self, city: str | None = None, limit: int = 50,
                 since: str | None = None, min_size: int = 1) -> Stats:
        stats = Stats()
        self.reoffer()
        for cluster_id in store.pending_clusters(self.db, city=city, limit=limit,
                                                 since=since, min_size=min_size):
            try:
                stats.add(self.process(cluster_id))
            except LlmUnavailable:
                # Модель недоступна ВСЕМ: следующий кластер и следующий город
                # её не оживят. Поднимаем наружу — проход прекращается целиком.
                raise
            except LlmError as exc:                 # исчерпан бюджет — дальше нет смысла
                log.error("остановка: %s", exc)
                break
            except Exception as exc:                # noqa: BLE001 — один кластер не роняет проход
                log.exception("кластер #%s: %s", cluster_id, exc)
                stats.processed += 1
                stats.errors += 1
        return stats


    def reoffer(self) -> int:
        """§28: кластер, отклонённый из-за нехватки фактуры, которому фактура
        пришла (первое медиа или +2 источника), возвращается в очередь разбора.

        Рерайт делается заново — материал у кластера уже другой. «Не новость»
        сюда не попадает никогда: это решение по новости, а не по посту."""
        returned = 0
        for cluster_id in actions.reoffer_candidates(self.db):
            if actions.supersede(self.db, cluster_id):
                log.info("кластер #%s предложен повторно: пришла новая фактура (§28)",
                         cluster_id)
                returned += 1
        return returned

    def _media_path(self, draft) -> str | None:
        """Ступень 3 каскада рисуется здесь же: модератор должен увидеть карточку,
        с которой пост выйдет, а не строку «шаблонная обложка»."""
        if draft.media.kind != "template":
            return draft.media.path
        path = render_cover(draft.city, draft.category, district=draft.district or "",
                            title=draft.title, out_dir=self.settings.cover_dir)
        return str(path) if path else None

    def _record_usage(self, purpose: str, model: str, usage) -> None:
        city, cluster_id = self._context
        self._spent = round(self._spent + getattr(usage, "cost_usd", 0.0), 6)
        store.log_llm_usage(self.db, purpose=purpose, model=model, city=city,
                            cluster_id=cluster_id,
                            input_tokens=getattr(usage, "input_tokens", 0),
                            output_tokens=getattr(usage, "output_tokens", 0),
                            cache_read=getattr(usage, "cache_read", 0),
                            cache_write=getattr(usage, "cache_write", 0),
                            cost_usd=getattr(usage, "cost_usd", 0.0))


def _city_of(db, cluster_id: int) -> str:
    row = db.conn.execute("SELECT city FROM clusters WHERE id=?", (cluster_id,)).fetchone()
    return row["city"] if row else ""


def _decided(db, cluster_id: int) -> bool:
    row = db.conn.execute("SELECT relevance FROM clusters WHERE id=?", (cluster_id,)).fetchone()
    return bool(row and row["relevance"])


def _notes(draft, safety) -> str:
    parts = []
    if draft.note:
        parts.append(draft.note)
    if draft.unsupported:
        parts.append("нет в источниках: " + ", ".join(draft.unsupported[:8]))
    if safety.reason:
        parts.append(f"safety: {safety.reason}")
    if draft.media.reason in ("watermarked", "unverified"):
        parts.append(f"медиа: {draft.media.reason}")
    return " · ".join(parts)
