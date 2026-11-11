"""Параметры relevance-фильтра (config/settings.yaml → блок `relevance`)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RelevanceSettings:
    enabled: bool = True
    # Классификатор можно выключить: правила продолжат работать бесплатно.
    use_llm: bool = True
    # Сколько символов фактуры кластера показываем классификатору. Тема поста
    # видна в первых абзацах, а платим мы за каждый токен.
    max_chars: int = 900
    max_tokens: int = 200
    # Отсекать ли посты, которые классификатор счёл нерелевантными городу.
    # Выключение оставляет только рекламу/конкурсы/гороскопы.
    drop_off_topic: bool = True
