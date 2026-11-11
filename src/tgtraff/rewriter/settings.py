"""Параметры rewriter'а (config/settings.yaml → блок `rewriter`)."""
from __future__ import annotations

from dataclasses import dataclass

# Категории поста — спека §5. Обложки R0 нарисованы для пяти из них;
# crime и other выходят на обложке категории «город» (см. QUESTIONS фазы 4).
CATEGORIES = ("dtp", "fire", "crime", "utility", "weather", "city", "other")
URGENCIES = ("flash", "normal")


@dataclass(frozen=True)
class RewriterSettings:
    enabled: bool = True
    max_tokens: int = 1200
    # Сколько символов текста одного источника уходит в промпт: длинный хвост
    # пресс-релиза фактуры не добавляет, а токены стоит.
    max_chars_per_post: int = 1800
    max_posts: int = 8
    # Пути к стайлгайду и few-shot: канал/город меняется конфигом, а не кодом
    # (требование CLAUDE.md о мультиканальности).
    style_path: str | None = None
    few_shot_path: str | None = None
    # Проверка «нет ли в драфте фактов, которых нет в кластере».
    check_facts: bool = True
    # Ниже этого объёма очищенной фактуры кластер помечается low_facts
    # (только ручная модерация). Живая лента: RSS-заголовок без тела — 40-70 знаков.
    min_fact_chars: int = 80
