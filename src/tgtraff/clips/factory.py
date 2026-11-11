"""Проход завода клипов (VIDEO-SPEC, фаза V1).

    драфты (safe, опубликованные в ТГ) → клип-фильтр → выбор медиа →
    клип-скоринг (LLM) → сценарий (LLM) → очередь рендера → карточка

Два правила, из-за которых конвейер устроен именно так:

  1. КАЖДЫЙ драфт разбирается ОДИН раз. Решение — сделали клип, пропустили,
     не смогли — записывается строкой в `clips`. Иначе завод платит модели
     за одни и те же кластеры каждые пять минут: ровно эта ловушка описана
     в QUESTIONS §70 про автопост.
  2. Рендер идёт очередью по одному: ffmpeg на 2 vCPU, которые мы делим
     с дедупом. Параллельность 1 — параметр конфига, а не случайность.

Дневной лимит (`clips_per_day`) считается на ГОРОД и по местным суткам — как
дневной потолок постов.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from tgtraff import drafts as drafts_store
from tgtraff.clips import clipfilter, store
from tgtraff.clips.media import choose as choose_media
from tgtraff.clips.script import ScriptError
from tgtraff.clips.settings import ClipSettings
from tgtraff.llm.client import LlmError, LlmUnavailable
from tgtraff.rewriter.material import collect_material

log = logging.getLogger("tgtraff.clips.factory")


@dataclass(frozen=True)
class ClipOutcome:
    draft_id: int
    clip_id: int | None
    status: str            # made | skipped | failed
    reason: str = ""
    cost_usd: float = 0.0


class ClipFactory:
    def __init__(self, db, settings: ClipSettings | None = None, scorer=None, writer=None,
                 builder=None, detector=None, pick_media=None, city_names=None,
                 usernames=None, city_tz=None):
        self.db = db
        self.settings = settings or ClipSettings()
        self.scorer = scorer
        self.writer = writer
        self.builder = builder
        self.detector = detector
        self._pick_media = pick_media
        self.city_names = city_names or {}
        self.usernames = usernames or {}
        self.city_tz = city_tz or {}

    # ---------- кандидаты ----------

    def candidates(self, now: datetime | str | None = None, limit: int | None = None,
                   any_status: bool = False) -> list:
        """Опубликованные safe-драфты без клипа, свежие вперёд.

        `NOT EXISTS` по `clips` — это и есть «каждый драфт разбирается один
        раз»: пропущенный, отклонённый и не собравшийся сюда не возвращаются
        (вернуть их можно руками, через CLI).

        Драфт без кластера в кандидаты не идёт: сервисные автопосты (погода,
        вечерний топ-5) — наш собственный текст без единого кадра, снимать
        в клип нечего. UGC-драфты тоже без кластера — их медиа лежит иначе,
        и клипы из UGC оставлены на потом (вопрос в QUESTIONS).

        `any_status` — только для калибровки и приёмки: в тестовом режиме
        каналов почти всё лежит в очереди модерации, а посмотреть качество
        клипов надо ДО боевого переключения. Ослабляется ровно одно условие —
        «опубликован»; вердикт safety и клип-фильтр остаются на месте."""
        moment = _moment(now)
        since = store.iso(moment - timedelta(hours=self.settings.max_age_hours))
        favorites = self.settings.favorite_categories or ()
        marks = ",".join("?" * len(favorites)) or "''"
        status = "d.status IN ('published','approved','deferred','new')" if any_status \
            else "d.status='published'"
        return self.db.conn.execute(
            f"""SELECT d.* FROM drafts d
                WHERE {status} AND d.safety_verdict='safe'
                  AND d.cluster_id IS NOT NULL
                  AND d.created_at >= ?
                  AND NOT EXISTS (SELECT 1 FROM clips c WHERE c.draft_id = d.id)
                ORDER BY (COALESCE(d.category,'other') IN ({marks})) DESC,
                         d.created_at DESC, d.id DESC
                LIMIT ?""",
            (since, *favorites, limit or self.settings.score_per_pass)).fetchall()

    # ---------- проход ----------

    def plan_clips(self, now: datetime | str | None = None,
                   any_status: bool = False) -> list[ClipOutcome]:
        """Скоринг и сценарий для кандидатов. Файл здесь не собирается —
        клип уходит в очередь рендера."""
        if not self.settings.enabled:
            return []
        moment = _moment(now)
        out: list[ClipOutcome] = []
        quota: dict[str, int] = {}
        for row in self.candidates(now=moment, any_status=any_status):
            city = row["city"]
            if city not in quota:
                quota[city] = store.made_today(self.db, city, self._tz(city),
                                               store.iso(moment))
            if quota[city] >= self.settings.clips_per_day:
                # Лимит дня — не решение по драфту: ничего не записываем.
                log.debug("город %s выбрал дневной лимит клипов (%s)", city,
                          self.settings.clips_per_day)
                continue
            try:
                outcome = self._one(row, moment)
            except LlmUnavailable as exc:
                # Модель недоступна ВСЕМ — следующий кандидат не поможет.
                log.warning("проход завода клипов остановлен: %s", exc)
                out.append(ClipOutcome(row["id"], None, "failed", str(exc)))
                break
            out.append(outcome)
            if outcome.status == "made":
                quota[city] += 1
        if out:
            made = sum(1 for o in out if o.status == "made")
            log.info("завод клипов: разобрано %s, сделано %s, потрачено $%.4f",
                     len(out), made, sum(o.cost_usd for o in out))
        return out

    def make_clip(self, row, now: datetime | str | None = None) -> ClipOutcome:
        """Разобрать ОДИН драфт: фильтр → медиа → скоринг → сценарий → очередь.

        Публичная: тем же путём ходит `scripts/clips.py --draft N`, когда клип
        просят собрать по конкретной новости."""
        return self._one(row, _moment(now))

    def _one(self, row, moment: datetime) -> ClipOutcome:
        draft_id = row["id"]
        city = row["city"]

        verdict = clipfilter.check(row, blocked=self.settings.blocked_categories)
        if not verdict.ok:
            return self._skip(row, verdict.reason)

        try:
            material = collect_material(self.db, row["cluster_id"],
                                        city_name=self.city_names.get(city, city))
        except LookupError:
            return self._skip(row, "кластера уже нет — собирать клип не из чего")

        media = self._media(material)
        if media is None:
            return self._skip(row, "нет пригодного медиа: чистого видео ≥"
                                   f"{self.settings.min_video_seconds:.0f} с и "
                                   f"{self.settings.min_photos} чистых фото не нашлось")

        cost = 0.0
        try:
            score = self.scorer.score(row, media, city_name=self.city_names.get(city, city))
            cost += score.cost_usd
            if not score.passes(self.settings.min_score):
                return self._skip(row, f"скоринг {score.score} ниже порога "
                                       f"{self.settings.min_score}: {score.reason}",
                                  score=score.score, cost=cost)
            script = self.writer.write(row, media,
                                       city_name=self.city_names.get(city, city),
                                       username=self.usernames.get(city))
            cost += script.cost_usd
        except LlmUnavailable:
            raise
        except (LlmError, ScriptError) as exc:
            # Сбой на конкретном драфте. Записываем решение: без записи завод
            # вернётся к нему через пять минут и заплатит снова.
            return self._skip(row, f"модель не дала сценарий: {exc}", cost=cost)

        # Пометки драфта (`unsupported_facts`) клип не запрещают, но в карточку
        # ехать обязаны: предупреждение автосверки не имеет права потеряться.
        draft_warnings = [f for f in drafts_store.decode_list(row["flags"])
                          if f in clipfilter.WARNING_FLAGS]
        flags = _unique([*draft_warnings, *media.flags, *script.flags])
        if not self.usernames.get(city):
            # Аутро без @ника — выброшенный клип: зрителю некуда идти. В тестовом
            # режиме каналов юзернейма нет вовсе (channels.yaml), и узнать об
            # этом модератор должен из карточки, а не из готового файла.
            flags.append("no_username")
        clip_id = store.save_clip(
            self.db, draft_id=draft_id, city=city, mode=media.mode, score=score.score,
            score_reason=score.reason, hook=script.hook, lines=script.lines,
            cta=script.cta_text, voice=self.settings.tts_voice if media.mode == "photos" else None,
            flags=flags, media_kind=media.kind, media_ids=media.media_ids,
            source_key=media.source_key, model=script.model or score.model, cost_usd=cost)
        log.info("клип #%s: драфт #%s, %s, скор %s, режим %s", clip_id, draft_id, city,
                 score.score, media.mode)
        return ClipOutcome(draft_id, clip_id, "made", script.hook, cost)

    def _media(self, material):
        if self._pick_media is not None:
            return self._pick_media(material, settings=self.settings,
                                    detector=self.detector, db=self.db)
        return choose_media(material, self.settings, detector=self.detector, db=self.db)

    def _skip(self, row, reason: str, score: int | None = None,
              cost: float = 0.0) -> ClipOutcome:
        """Запись «клипа не будет и почему». Она же — защита от повторной
        оплаты: этот драфт в кандидаты больше не попадёт."""
        clip_id = store.save_clip(self.db, draft_id=row["id"], city=row["city"], mode="none",
                                  score=score, score_reason=reason, cost_usd=cost)
        self.db.conn.execute(
            "UPDATE clips SET status='skipped', reject_reason=? WHERE id=?", (reason, clip_id))
        log.info("клипа по драфту #%s не будет: %s", row["id"], reason)
        return ClipOutcome(row["id"], clip_id, "skipped", reason, cost)

    # ---------- перегенерация хука ----------

    def rescript_pending(self, limit: int = 3) -> list[ClipOutcome]:
        """Кнопка «Перегенерить хук» (VIDEO-SPEC): модератор пометил клип, а
        платный запрос делает завод — ключ и промпты живут в одном месте.

        Предыдущий хук уходит в промпт: без него модель нередко возвращает
        ровно то же самое, и кнопка выглядит неработающей."""
        if not self.settings.enabled:
            return []
        out: list[ClipOutcome] = []
        for clip in store.pending_rehook(self.db, limit=limit):
            draft = self.db.conn.execute("SELECT * FROM drafts WHERE id=?",
                                         (clip["draft_id"],)).fetchone()
            if draft is None:
                store.mark_failed(self.db, clip["id"], "драфта уже нет", max_attempts=0)
                continue
            city = clip["city"]
            media = _MediaHint(clip["mode"], clip["media_ids"])
            try:
                script = self.writer.write(
                    draft, media, city_name=self.city_names.get(city, city),
                    username=self.usernames.get(city),
                    hint=f"дай ДРУГОЙ хук, предыдущий не подошёл: «{clip['hook']}»")
            except LlmUnavailable as exc:
                log.warning("перегенерация хука отложена: %s", exc)
                break
            except (LlmError, ScriptError) as exc:
                log.warning("хук клипа #%s не перегенерился (%s) — вернули как был",
                            clip["id"], exc)
                store.restore_ready(self.db, clip["id"])
                out.append(ClipOutcome(clip["draft_id"], clip["id"], "failed", str(exc)))
                continue
            store.update_script(self.db, clip["id"], hook=script.hook, lines=script.lines,
                                cta=script.cta_text, model=script.model,
                                cost_usd=script.cost_usd)
            log.info("клип #%s: новый хук «%s»", clip["id"], script.hook)
            out.append(ClipOutcome(clip["draft_id"], clip["id"], "made", script.hook,
                                   script.cost_usd))
        return out

    # ---------- очередь рендера ----------

    def render_next(self) -> ClipOutcome | None:
        """Собирает ОДИН клип. Параллельность 1 держится здесь: за проход
        берётся ровно одна запись, и берётся она claim'ом."""
        if not self.settings.enabled:
            return None
        store.release_stuck(self.db, older_than_minutes=_stuck_minutes(self.settings))
        rows = store.pending_render(self.db, limit=1)
        if not rows:
            return None
        clip = rows[0]
        if not store.claim_render(self.db, clip["id"]):
            return None                            # забрал другой процесс

        draft = self.db.conn.execute("SELECT * FROM drafts WHERE id=?",
                                     (clip["draft_id"],)).fetchone()
        files = self._files(clip)
        if draft is None or not files:
            store.mark_failed(self.db, clip["id"], "исходные медиа пропали с диска",
                              max_attempts=0)
            return ClipOutcome(clip["draft_id"], clip["id"], "failed", "медиа пропали")

        result = self.builder.build(clip, draft, files,
                                    city_name=self.city_names.get(clip["city"]),
                                    username=self.usernames.get(clip["city"]))
        if not result.ok:
            status = store.mark_failed(self.db, clip["id"], result.error,
                                       max_attempts=self.settings.max_attempts)
            log.warning("клип #%s не собрался (%s): %s", clip["id"], status, result.error)
            return ClipOutcome(clip["draft_id"], clip["id"], "failed", result.error)

        store.mark_rendered(self.db, clip["id"], path=str(result.path), size=result.size,
                            duration=result.duration,
                            subtitles=getattr(result, "subtitles", "none"))
        log.info("клип #%s собран: %.0f с, %.1f МБ, субтитры %s", clip["id"],
                 result.duration, result.size / 1048576, getattr(result, "subtitles", "none"))
        return ClipOutcome(clip["draft_id"], clip["id"], "made", str(result.path))

    def render_pending(self, limit: int = 3) -> list[ClipOutcome]:
        """Несколько клипов ПОДРЯД, но всё так же по одному за раз."""
        out: list[ClipOutcome] = []
        for _ in range(limit):
            outcome = self.render_next()
            if outcome is None:
                break
            out.append(outcome)
        return out

    def _files(self, clip) -> list[str]:
        """Пути исходников по id медиа. Читаем из базы, а не из строки клипа:
        ретеншн мог убрать файл, и тогда клип честно провалится."""
        ids = [int(x) for x in store.decode_lines(clip["media_ids"]) if str(x).isdigit()]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self.db.conn.execute(
            f"SELECT id, file_path FROM raw_media WHERE id IN ({marks})", ids).fetchall()
        found = {r["id"]: r["file_path"] for r in rows if r["file_path"]}
        from pathlib import Path

        return [found[i] for i in ids if i in found and Path(found[i]).exists()]

    def _tz(self, city: str) -> str:
        return self.city_tz.get(city, "Europe/Moscow")


@dataclass(frozen=True)
class _MediaHint:
    """Чем клип уже собран — столько сценаристу и нужно знать при перегенерации
    (сами файлы для нового текста не требуются)."""

    mode: str
    ids: str | None = None
    duration: float | None = None
    has_audio: bool = True

    @property
    def files(self) -> list[str]:
        return store.decode_lines(self.ids)


def _unique(values) -> list[str]:
    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def _moment(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        return store.parse(value)
    return value.astimezone(timezone.utc)


def _stuck_minutes(settings: ClipSettings) -> int:
    """Клип считается застрявшим, когда прошло заметно больше отведённого
    рендеру времени: ffmpeg на слабом ядре идёт долго и это норма."""
    return max(int(settings.render_timeout_sec / 60) * 2, 15)
