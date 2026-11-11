"""Медиа-каскад (спека §5) — правило выбора иллюстрации, нарушать нельзя.

    1. UGC из нашего бота / медиа официальных источников (МЧС, ГИБДД,
       прокуратура, администрация) — высший приоритет.
    2. Медиа без чужих вотермарков из любого источника кластера.
    3. Чистого нет → своя шаблонная обложка (Pillow) или пост без медиа.

Детектор вотермарков зовётся лениво, в порядке каскада, и останавливается на
первом чистом кандидате: вердикт по картинке всё равно кэшируется в базе, а
проверять вслепую все медиа крупного кластера — деньги на ветер (см. QUESTIONS
фазы 4). Верхняя граница числа проверок — параметр `max_checks`.

Видео vision-детектор не смотрит: оно попадает в драфт с пометкой
«не проверено», и решение остаётся за модератором.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from tgtraff import drafts as drafts_store
from tgtraff.rewriter.watermark import FOREIGN, NONE, OWN, UNKNOWN

log = logging.getLogger("tgtraff.rewriter.media")

# Ведомственные знаки на ведомственном же фото — не чужой вотермарк: эти
# источники сами распространяют снимки для перепечатки.
_AGENCY_WORDS = ("мчс", "гибдд", "мвд", "прокурат", "следств", "администрац", "мэри")


@dataclass(frozen=True)
class MediaSettings:
    use_template_cover: bool = True
    # Сколько картинок кластера имеем право отправить в vision за один драфт.
    max_checks: int = 4
    # Видео без проверки вотермарка предлагать модератору или прятать.
    allow_unverified_video: bool = True


@dataclass(frozen=True)
class MediaChoice:
    kind: str                       # photo | video | template | none
    reason: str                     # official | ugc | clean | unverified | watermarked | no_media
    path: str | None = None
    media_id: int | None = None
    watermark: str | None = None
    owner: str | None = None
    needs_check: bool = False       # модератор обязан глянуть сам
    checked: int = 0                # сколько платных проверок ушло на этот драфт

    @property
    def has_file(self) -> bool:
        return bool(self.path)


def choose_media(material, detector, settings: MediaSettings | None = None,
                 db=None) -> MediaChoice:
    settings = settings or MediaSettings()
    media = [m for m in material.media if m.is_ready]
    if not media:
        return _fallback(settings, "no_media")

    official = [m for m in media if m.official]
    photos = [m for m in media if not m.official and m.kind == "photo"]
    videos = [m for m in media if not m.official and m.kind in ("video", "animation")]

    # Ступень 1. Ведомство и UGC проверять не нужно: знак на таком фото свой.
    if official:
        best = _prefer_photo(official)
        return MediaChoice(kind=best.kind, reason="official", path=best.file_path,
                           media_id=best.media_id, watermark=best.watermark,
                           owner=best.watermark_owner)

    # Ступень 2. Первое фото без чужого вотермарка.
    checks = 0
    for item in photos:
        verdict, owner = item.watermark, item.watermark_owner
        if verdict is None:
            if checks >= settings.max_checks:
                break
            verdict, owner = detector.verdict(item)
            checks += 1
            if db is not None:
                drafts_store.set_media_watermark(db, item.media_id, verdict, owner)
        if _is_acceptable(verdict, owner, item):
            return MediaChoice(kind="photo", reason="clean", path=item.file_path,
                               media_id=item.media_id, watermark=verdict, owner=owner,
                               needs_check=verdict == UNKNOWN, checked=checks)

    # Видео детектор не смотрит — отдаём модератору с пометкой.
    if videos and settings.allow_unverified_video:
        best = videos[0]
        return MediaChoice(kind=best.kind, reason="unverified", path=best.file_path,
                           media_id=best.media_id, needs_check=True, checked=checks)

    # Ступень 3. Чистого нет — своя обложка (или пост без медиа).
    return _fallback(settings, "watermarked", checked=checks)


def _fallback(settings: MediaSettings, reason: str, checked: int = 0) -> MediaChoice:
    if settings.use_template_cover:
        return MediaChoice(kind="template", reason=reason, checked=checked)
    return MediaChoice(kind="none", reason=reason, checked=checked)


def _prefer_photo(items):
    return next((m for m in items if m.kind == "photo"), items[0])


def _is_acceptable(verdict: str | None, owner: str | None, item) -> bool:
    """Годится всё, кроме доказанного чужого знака.

    `unknown` (не смогли проверить) не роняет драфт в шаблон: пост уйдёт
    модератору с пометкой «медиа не проверено» — в фазе 4 автопубликации нет."""
    if verdict == FOREIGN:
        return is_own_agency_mark(owner, item)
    return verdict in (NONE, OWN, UNKNOWN, None)


def is_own_agency_mark(owner: str | None, item) -> bool:
    """Знак МЧС на фото самого МЧС — это подпись автора, а не чужой вотермарк.

    Публичная: то же правило нужно заводу клипов (clips/media.py), и жить
    оно должно в одном месте."""
    if not owner or not item.official:
        return False
    low = owner.lower()
    return any(word in low for word in _AGENCY_WORDS)
