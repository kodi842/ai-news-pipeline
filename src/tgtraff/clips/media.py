"""Выбор медиа под клип (VIDEO-SPEC, жёсткое правило 2).

Каскад тот же, что у постов (спека §5): UGC/официальное → чистое из кластера.
Разница в конце: у поста третья ступень — своя шаблонная обложка, и пост
выходит всегда. У клипа третьей ступени НЕТ. Нет пригодных кадров — нет клипа
(PROJECT-SPEC: «нет медиа — клип не делается»), потому что клип с чужим знаком
в рекомендациях площадки — это витрина чужого бренда и заявка на жалобу.

Строгость против каскада постов:
  * фото с непроверяемым знаком (`unknown`) не берётся вовсе — у поста оно
    уходит модератору с пометкой. Фото в кластере обычно много, клип один;
  * ВИДЕО проверяется по КАДРУ: vision-детектор видео не смотрит, поэтому из
    файла вынимается кадр и проверяется он. Если кадр вытащить не удалось или
    детектора нет — клип собирается с флагом `media_unverified`, и человек в
    V1 смотрит его сам (автопаба клипов не существует).

Чего в модуле нет: снятия, кропа и затирания чужих знаков — ст. 1300 ГК и
принцип проекта. Детектор умеет ровно одно: сказать, что кадр чужой.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

from tgtraff import drafts as drafts_store
from tgtraff.clips.settings import ClipSettings
from tgtraff.rewriter.media import is_own_agency_mark
from tgtraff.rewriter.watermark import FOREIGN, NONE, OWN, UNKNOWN

log = logging.getLogger("tgtraff.clips.media")

VIDEO_KINDS = ("video", "animation")
UNVERIFIED = "media_unverified"


@dataclass(frozen=True)
class ClipMedia:
    """Исходники клипа: либо одно видео, либо 2-5 фото."""

    mode: str                       # video | photos
    kind: str                       # video | photo
    files: list[str]
    media_ids: list[int]
    source_key: str
    reason: str                     # official | clean
    duration: float | None = None   # длительность исходного видео
    has_audio: bool = False
    flags: list[str] = field(default_factory=list)
    checked: int = 0                # сколько платных проверок ушло


def choose(material, settings: ClipSettings | None = None, detector=None,
           probe=None, grab_frame=None, db=None) -> ClipMedia | None:
    """Что показать в клипе. None — клипа не будет.

    `probe` и `grab_frame` подставляются (тесты и разные пути к ffmpeg):
    probe(path) → VideoInfo|None, grab_frame(path, at=..) → путь к кадру|None.
    """
    settings = settings or ClipSettings()
    probe = probe or _default_probe(settings)
    grab_frame = grab_frame or _default_grab(settings)

    ready = [m for m in material.media if m.is_ready]
    if not ready:
        return None

    checks = _Budget(settings.max_checks)

    video = _pick_video(ready, settings, detector, probe, grab_frame, db, checks)
    if video is not None:
        return video
    return _pick_photos(ready, settings, detector, db, checks)


# ---------- видео ----------

def _pick_video(ready, settings, detector, probe, grab_frame, db, checks) -> ClipMedia | None:
    for item in _cascade(ready, VIDEO_KINDS):
        info = probe(item.file_path)
        if info is None:
            log.debug("видео %s не прочиталось ffprobe — в клип не берём", item.file_path)
            continue
        if info.duration < settings.min_video_seconds:
            log.debug("видео %s короче %.0f с (%.1f) — не кандидат",
                      item.file_path, settings.min_video_seconds, info.duration)
            continue

        verdict, owner, spent = _video_verdict(item, detector, grab_frame, db, settings, checks)
        if verdict == FOREIGN and not is_own_agency_mark(owner, item):
            log.info("видео %s с чужим знаком (%s) — клипа из него не будет",
                     item.file_path, owner or "не разобрали")
            continue

        flags = [UNVERIFIED] if verdict == UNKNOWN else []
        return ClipMedia(mode="video", kind="video", files=[item.file_path],
                         media_ids=[item.media_id], source_key=item.source_key,
                         reason="official" if item.official else "clean",
                         duration=info.duration, has_audio=info.has_audio,
                         flags=flags, checked=spent)
    return None


def _video_verdict(item, detector, grab_frame, db, settings, checks):
    """Вердикт по видео: из базы, иначе по кадру. Кадр — единственный способ
    показать видео vision-детектору."""
    if item.watermark:
        return item.watermark, item.watermark_owner, 0
    if detector is None or not checks.allow():
        return UNKNOWN, None, 0
    frame = grab_frame(item.file_path, at=settings.watermark_frame_at)
    if not frame:
        return UNKNOWN, None, 0
    verdict, owner = detector.verdict(replace(item, kind="photo", file_path=str(frame)))
    checks.spend()
    if db is not None and verdict != UNKNOWN:
        # Кэш на строке медиа: одно видео — один платный запрос за его жизнь.
        drafts_store.set_media_watermark(db, item.media_id, verdict, owner)
    return verdict, owner, 1


# ---------- фото ----------

def _pick_photos(ready, settings, detector, db, checks) -> ClipMedia | None:
    files: list[str] = []
    ids: list[int] = []
    source_key = ""
    official = False
    for item in _cascade(ready, ("photo",)):
        verdict, owner = item.watermark, item.watermark_owner
        if verdict is None and detector is not None and checks.allow():
            verdict, owner = detector.verdict(item)
            checks.spend()
            if db is not None:
                drafts_store.set_media_watermark(db, item.media_id, verdict, owner)
        if not _photo_is_clean(verdict, owner, item):
            continue
        files.append(item.file_path)
        ids.append(item.media_id)
        source_key = source_key or item.source_key
        official = official or item.official
        if len(files) >= settings.max_photos:
            break
    if len(files) < settings.min_photos:
        return None
    return ClipMedia(mode="photos", kind="photo", files=files, media_ids=ids,
                     source_key=source_key, reason="official" if official else "clean",
                     checked=checks.spent)


def _photo_is_clean(verdict, owner, item) -> bool:
    """Строже каскада постов: годится только доказанно чистое фото.

    `unknown` (не проверили или проверить не смогли) в клип не идёт: у поста
    такой кадр уходит модератору с пометкой, но клип — витрина, и рисковать
    чужим знаком в рекомендациях мы не будем."""
    if verdict == FOREIGN:
        return is_own_agency_mark(owner, item)
    return verdict in (NONE, OWN)


# ---------- служебное ----------

class _Budget:
    """Сколько платных проверок разрешено на один клип."""

    def __init__(self, limit: int):
        self.limit = limit
        self.spent = 0

    def allow(self) -> bool:
        return self.spent < self.limit

    def spend(self) -> None:
        self.spent += 1


def _cascade(ready, kinds):
    """Ступени каскада: сначала ведомство/UGC, потом остальные."""
    items = [m for m in ready if m.kind in kinds]
    return [m for m in items if m.official] + [m for m in items if not m.official]


def _default_probe(settings: ClipSettings):
    from tgtraff.clips.probe import probe_video

    return lambda path: probe_video(path, ffprobe=settings.ffprobe)


def _default_grab(settings: ClipSettings):
    from tgtraff.clips.probe import grab_frame

    return lambda path, at=1.5: grab_frame(path, at=at, out_dir=settings.work_dir,
                                           ffmpeg=settings.ffmpeg)
