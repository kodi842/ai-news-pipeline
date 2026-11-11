"""Клип-скоринг (VIDEO-SPEC): виральный потенциал новости от 1 до 10.

Зачем платить модели за оценку, а не брать всё подряд: рендер стоит минуты
процессора, а место в рекомендациях — репутации. Слабый клип не просто не
зайдёт — он портит показы следующим (площадка учит алгоритм на досмотрах).

Модель получает не только текст, но и то, ЧЕМ мы располагаем: живое видео на
27 секунд и слайдшоу из двух фото — разный потенциал у одной и той же новости.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from tgtraff.clips.settings import ClipSettings
from tgtraff.llm.client import LlmError

log = logging.getLogger("tgtraff.clips.score")

SYSTEM = """Ты продюсер коротких вертикальных видео городского канала
происшествий. Оцениваешь, стоит ли из новости делать клип для VK Клипов,
Rutube и OK: попадёт ли он в рекомендации и досмотрят ли его до конца.

Шкала виральности 1-10:
  9-10 — зрелищное живое видео события, которое обсуждает весь город;
  7-8  — понятное городское ЧП с кадрами с места, узнаваемое место;
  5-6  — обычная новость, кадры есть, но смотреть особо нечего;
  3-4  — объявление, статистика, официальный отчёт;
  1-2  — смотреть нечего вообще: кадры не по делу или тема мимо города.

Что поднимает оценку: динамика в кадре (огонь, вода, техника, работа служб),
узнаваемое место города, «это могло случиться со мной», редкость события,
понятная одной фразой суть.
Что опускает: постановочные и статичные кадры, канцелярит, отчётность,
пересказ без картинки, тема, интересная только узкому кругу.

Категории-приоритеты сетки: {favorites}.

Верни ТОЛЬКО JSON: {{"score": 1-10, "reason": "одна строка — почему"}}"""


@dataclass(frozen=True)
class ClipScore:
    score: int
    reason: str
    model: str = ""
    cost_usd: float = 0.0

    def passes(self, threshold: int) -> bool:
        return self.score >= threshold


class ClipScorer:
    def __init__(self, client, settings: ClipSettings | None = None):
        self.client = client
        self.settings = settings or ClipSettings()

    @property
    def system_prompt(self) -> str:
        return SYSTEM.format(favorites=", ".join(self.settings.favorite_categories))

    def score(self, draft, media, city_name: str | None = None) -> ClipScore:
        data, reply = self.client.ask_json(
            system=self.system_prompt, user=self._prompt(draft, media, city_name),
            max_tokens=200, purpose="clip_score")
        raw = data.get("score")
        value = _int(raw)
        if value is None:
            raise LlmError(f"скоринг без оценки: {data!r}")
        return ClipScore(score=max(1, min(10, value)),
                         reason=str(data.get("reason") or "").strip(),
                         model=getattr(reply, "model", ""),
                         cost_usd=getattr(reply, "cost_usd", 0.0))

    def _prompt(self, draft, media, city_name: str | None) -> str:
        city = city_name or _get(draft, "city") or ""
        lines = [f"Город: {city}",
                 f"Категория: {_get(draft, 'category') or 'other'}",
                 f"Район: {_get(draft, 'district') or 'не указан'}",
                 f"Материал клипа: {_media_line(media)}",
                 "",
                 f"Заголовок: {_get(draft, 'title') or ''}",
                 f"Текст: {_get(draft, 'body') or ''}",
                 "",
                 "Оцени виральный потенциал клипа по этой новости. Только JSON."]
        return "\n".join(lines)


def _media_line(media) -> str:
    if media is None:
        return "медиа нет"
    if media.mode == "video":
        tail = "со звуком" if media.has_audio else "без звука"
        return (f"живое видео с места, {media.duration:.0f} секунд, {tail}"
                if media.duration else f"живое видео с места, {tail}")
    return f"фотографий: {len(media.files)} — будет слайдшоу с озвучкой"


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _get(row, key: str):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None
