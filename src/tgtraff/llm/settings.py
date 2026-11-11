"""Параметры обращений к Haiku (config/settings.yaml → блок `llm`).

Спека §5: «Модель: Haiku… Обязательно: prompt caching для стайлгайда/few-shot,
правила до LLM, Batch API (−50%) для несрочного; молнии — только живыми
запросами». Бюджет фазы 4 (калибровка) — $1-3 суммарно, полный прод трёх
городов — $1.2-2/день; отсюда `budget_usd` как предохранитель процесса.
"""
from __future__ import annotations

from dataclasses import dataclass

# Прайс Anthropic, $ за 1M токенов. Кэш: запись ×1.25 от входа, чтение ×0.1.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
}
CACHE_WRITE_RATE = 1.25
CACHE_READ_RATE = 0.1


@dataclass(frozen=True)
class LlmSettings:
    enabled: bool = True
    model: str = "claude-haiku-4-5"
    # Vision-модель детектора вотермарков — та же Haiku (спека §5).
    vision_model: str = "claude-haiku-4-5"
    max_tokens: int = 1200
    timeout_sec: float = 60.0
    max_retries: int = 3
    # Предохранитель: процесс останавливается, а не сжигает бюджет молча.
    # 0 — без ограничения (в проде лимит держит сам конвейер).
    budget_usd: float = 0.0
    # Минимальный кэшируемый префикс у Haiku 4.5 — 4096 токенов: системный
    # промпт короче просто не закэшируется (молча, без ошибки).
    cache_system: bool = True

    def price(self, model: str | None = None) -> tuple[float, float]:
        return PRICES.get(model or self.model, PRICES["claude-haiku-4-5"])
