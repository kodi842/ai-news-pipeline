"""Параметры safety-фильтра (config/settings.yaml → блок `safety`)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SafetySettings:
    enabled: bool = True
    use_llm: bool = True
    max_tokens: int = 300
    max_chars: int = 2500
    # Спека §6: «safe-автопубликация выключена по умолчанию; включается вручную
    # по категориям после калибровки». Пустой список = автопубликации нет.
    autopublish_categories: list[str] = field(default_factory=list)

    def may_autopublish(self, verdict: str, category: str) -> bool:
        return verdict == "safe" and category in self.autopublish_categories
