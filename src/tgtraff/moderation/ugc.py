"""UGC-приём: новость от читателя → та же очередь модерации (спека, фаза 5).

Один бот на всю сеть (`@example_channel_3`), город приходит deep-link'ом
`?start=mgn|nvk|che` — так одна кнопка под постом раскладывает входящие по
городам (DECISIONS.md).

Присланное НЕ проходит рерайт: модератор видит текст читателя как есть и
правит его сам. Бесплатный словарь safety всё же прогоняется — стоп-тема из
чужих рук так же опасна, как из ленты.

Фото получает НАШ вотермарк (спека §5): это наш эксклюзив. Чужие знаки при
этом не трогаются никогда — ни кропа, ни затирания в конвейере нет.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from tgtraff import drafts as drafts_store
from tgtraff.safety import rules as safety_rules

log = logging.getLogger("tgtraff.ugc")

DEFAULT_TITLE = "Новость от читателя"
MAX_TITLE = 120


class UnknownCity(RuntimeError):
    """Читатель пришёл без deep-link'а — не знаем, в какой город его новость."""


class UgcIntake:
    def __init__(self, db, deep_links: dict[str, str], media_dir: Path | str,
                 stamper=None):
        self.db = db
        self.deep_links = deep_links
        self.media_dir = Path(media_dir)
        self._stamp = stamper or _brand_stamp

    # ---------- отправитель ----------

    def register(self, user_id: int, username: str | None, payload: str | None) -> str | None:
        """Запоминает город читателя по deep-link'у. Неизвестный payload — не
        ошибка: бот просто спросит город кнопками."""
        city = self.deep_links.get((payload or "").strip().lower())
        self.db.conn.execute(
            """INSERT INTO ugc_senders (user_id, username, city, first_seen_at, last_seen_at)
               VALUES (?,?,?, strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                              strftime('%Y-%m-%dT%H:%M:%SZ','now'))
               ON CONFLICT(user_id) DO UPDATE SET
                   username=excluded.username,
                   city=COALESCE(excluded.city, ugc_senders.city),
                   last_seen_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')""",
            (user_id, username, city))
        return city

    def set_city(self, user_id: int, city: str) -> None:
        self.db.conn.execute("UPDATE ugc_senders SET city=? WHERE user_id=?", (city, user_id))

    def sender(self, user_id: int):
        return self.db.conn.execute(
            "SELECT * FROM ugc_senders WHERE user_id=?", (user_id,)).fetchone()

    def city_of(self, user_id: int) -> str | None:
        row = self.sender(user_id)
        return row["city"] if row else None

    # ---------- приём ----------

    def submit(self, user_id: int, username: str | None, text: str,
               media_path: Path | str | None = None, media_kind: str | None = None,
               city: str | None = None) -> int:
        city = city or self.city_of(user_id)
        if not city:
            raise UnknownCity("город отправителя неизвестен")
        text = (text or "").strip()
        if not text and not media_path:
            raise ValueError("пустая заявка: ни текста, ни медиа")

        head, _, rest = text.partition("\n")
        title = (head.strip() or DEFAULT_TITLE)[:MAX_TITLE]
        body = rest.strip() or (text if not rest else "")

        verdict = safety_rules.check_text(text)
        stored = self._store_media(city, user_id, media_path, media_kind)

        draft_id = drafts_store.save_draft(
            self.db, cluster_id=None, city=city, title=title, body=body,
            category="other", urgency="normal", origin="ugc", flags=["ugc"],
            safety_verdict=verdict.verdict, safety_reasons=verdict.categories,
            media_kind=media_kind, media_path=str(stored) if stored else None,
            media_reason="ugc", ugc_user_id=user_id, ugc_username=username,
            notes=_notes(media_kind, verdict), status="new")
        self.db.conn.execute(
            "UPDATE ugc_senders SET submissions = submissions + 1, "
            "last_seen_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE user_id=?", (user_id,))
        log.info("UGC #%s от %s (%s): %s", draft_id, user_id, city, title[:60])
        return draft_id

    def _store_media(self, city: str, user_id: int, media_path, media_kind) -> Path | None:
        if not media_path:
            return None
        src = Path(media_path)
        out_dir = self.media_dir / "ugc" / city
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dst = out_dir / f"{stamp}-{user_id}{src.suffix.lower() or '.bin'}"
        if media_kind == "photo":
            try:
                self._stamp(src, dst, city)
                return dst
            except Exception as exc:              # noqa: BLE001 — знак не критичен
                log.warning("вотермарк на UGC не поставился (%s) — кладём как есть", exc)
        shutil.copyfile(src, dst)
        return dst


def _notes(media_kind: str | None, verdict) -> str:
    parts = ["прислано читателем (UGC)"]
    if media_kind == "video":
        # ffmpeg в пилот не тянем: видео уходит модератору без нашего знака.
        parts.append("видео не размечено нашим знаком")
    if verdict.categories:
        parts.append("safety: " + ", ".join(verdict.categories))
    return " · ".join(parts)


def _brand_stamp(src: Path, dst: Path, city: str) -> None:
    """Мостик к генератору знака из бренд-пакета R0."""
    import sys

    brand_dir = Path(__file__).resolve().parents[3] / "assets" / "brand"
    if str(brand_dir) not in sys.path:
        sys.path.insert(0, str(brand_dir))
    from PIL import Image
    import watermark                              # assets/brand/watermark.py

    watermark.stamp(Image.open(src), city).save(dst, quality=92)
