"""Фото от модератора ответом на карточку (решение ревью §61.1).

Ровно та же механика, что у правки текста, только предметом правки становится
картинка: модератор отвечает на карточку фотографией, и она заменяет медиа
драфта. Зачем: медиа-каскад (спека §5) выбирает лучшее из того, что есть в
кластере, — а у модератора бывает кадр, которого в кластере нет вовсе.

Два правила спеки §5 держатся здесь, а не в боте:
  * ЧУЖОЙ ЗНАК НЕ ТРОГАЕМ. Ни кропа, ни затирания — их в проекте нет и не
    будет (ст. 1300 ГК РФ). Присланное прогоняется через тот же vision-
    детектор, что и медиа кластера; чужой знак — повод спросить человека,
    а не повод «почистить» картинку.
  * НАШ ЗНАК — ТОЛЬКО НА ЧИСТЫЙ КАДР. Плашка «@example_channel_2» заявляет наш
    эксклюзив, и на чужом фото это была бы неправда. Поэтому подтверждённое
    чужое медиа кладётся как есть.

«Не проверено» приравнивается к «чужому»: детектор молчит, когда модель
недоступна, и молчание не должно означать «чисто».

Логика решений — здесь, aiogram-обвязка — в `bot.py`.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tgtraff import drafts as drafts_store
from tgtraff.rewriter.watermark import FOREIGN, NONE

log = logging.getLogger("tgtraff.moderation.photo")

# Причина выбора медиа для карточки и базы: этот кадр пришёл от человека,
# а не от каскада §5.
MEDIA_REASON = "moderator"


@dataclass(frozen=True)
class PhotoCheck:
    """Что детектор сказал о присланном кадре."""

    verdict: str
    owner: str | None = None

    @property
    def needs_confirmation(self) -> bool:
        """Публикуем без вопросов только то, что ЯВНО чисто. Чужой знак и
        несостоявшаяся проверка одинаково требуют решения человека."""
        return self.verdict != NONE

    @property
    def is_foreign(self) -> bool:
        return self.verdict == FOREIGN


class ModeratorPhoto:
    def __init__(self, db, media_dir: Path | str, detector=None, stamper=None):
        self.db = db
        self.media_dir = Path(media_dir)
        self.detector = detector
        self._stamp = stamper or _brand_stamp

    # ---------- проверка ----------

    def check(self, path: Path | str) -> PhotoCheck:
        """Прогоняет кадр через vision-детектор чужих знаков (спека §5).

        Детектора нет вовсе (не собран, нет ключа) — это тоже «не проверено»:
        решение уходит человеку, а не выдаётся за чистый кадр."""
        if self.detector is None:
            return PhotoCheck("unknown")
        try:
            verdict, owner = self.detector.verdict(_as_media(path))
        except Exception as exc:                  # noqa: BLE001 — модель, сеть, битый файл
            log.warning("детектор знаков не ответил по фото модератора: %s", exc)
            return PhotoCheck("unknown")
        return PhotoCheck(verdict, owner)

    # ---------- подстановка ----------

    def attach(self, draft_id: int, src: Path | str, moderator_id: int,
               stamp: bool = True) -> Path:
        """Кладёт кадр в медиа-каталог и делает его медиа драфта.

        `stamp=False` — подтверждённое чужое фото: своим знаком не метим.
        Файл модератора остаётся нетронутым, у каждого кадра своё имя:
        перевыпуск вышедшего поста ссылается на конкретный файл."""
        row = drafts_store.get_draft(self.db, draft_id)
        if row is None:
            raise LookupError(f"драфта #{draft_id} нет в базе")
        stored = self._store(row["city"], Path(src), moderator_id, stamp)
        note = f"фото модератора {moderator_id}"
        self.db.conn.execute(
            f"""UPDATE drafts SET media_kind='photo', media_reason=?, media_path=?,
                    media_raw_media_id=NULL, notes=?, updated_at={drafts_store.NOW_SQL}
                WHERE id=?""",
            (MEDIA_REASON, str(stored), _append_note(row["notes"], note), draft_id))
        log.info("медиа драфта #%s заменено модератором %s: %s", draft_id, moderator_id, stored)
        return stored

    def _store(self, city: str, src: Path, moderator_id: int, stamp: bool) -> Path:
        out_dir = self.media_dir / "moderator" / city
        out_dir.mkdir(parents=True, exist_ok=True)
        mark = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        dst = out_dir / f"{mark}-{moderator_id}{src.suffix.lower() or '.jpg'}"
        if stamp:
            try:
                self._stamp(src, dst, city)
                return dst
            except Exception as exc:              # noqa: BLE001 — знак не критичен, кадр критичен
                log.warning("наш знак на фото модератора не встал (%s) — кладём как есть", exc)
        shutil.copyfile(src, dst)
        return dst


def _as_media(path: Path | str):
    """Детектор ждёт объект медиа кластера — присланному кадру строки в базе
    нет, поэтому подаём ему ту же форму без записи."""
    from tgtraff.rewriter.material import MediaRef

    return MediaRef(media_id=0, raw_post_id=0, ord=0, kind="photo",
                    file_path=str(path), source_key="moderator", official=False)


def _append_note(existing: str | None, note: str) -> str:
    return f"{existing} · {note}" if existing else note


def _brand_stamp(src: Path, dst: Path, city: str) -> None:
    """Тот же мостик к бренд-пакету, что у UGC (`moderation/ugc.py`)."""
    from tgtraff.moderation.ugc import _brand_stamp as stamp

    stamp(src, dst, city)
