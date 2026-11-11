"""Safety-фильтр целиком: стоп-словарь + LLM-классификатор (спека §6).

Два контура и правило «берётся худший вердикт». Асимметрия ошибок здесь
обратная relevance-фильтру: там лишний драфт стоил клика, здесь пропущенная
тема стоит канала. Поэтому упавший API и непонятный ответ означают
`manual_only`, а не `safe`.

Проверяется ГОТОВЫЙ драфт, а не исходники: публикуется наш текст, и отвечать
мы будем за него.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from tgtraff.llm.client import LlmError, LlmUnavailable
from tgtraff.safety import rules
from tgtraff.safety.rules import MANUAL_ONLY, REJECT, SAFE, worst
from tgtraff.safety.settings import SafetySettings

log = logging.getLogger("tgtraff.safety")

VERDICTS = (SAFE, MANUAL_ONLY, REJECT)

SYSTEM = """Ты — редактор безопасности городского новостного Telegram-канала в России.
Тебе дают ГОТОВЫЙ текст поста. Реши, можно ли его публиковать.

Верни ТОЛЬКО JSON:
{"verdict": "safe|manual_only|reject", "categories": ["..."], "reason": "коротко"}

Вердикты:
  safe        — обычная городская новость: ДТП, пожар, коммуналка, погода,
                криминальная хроника, работа служб, городская повестка.
                Сообщение о погибших и пострадавших в происшествии — safe,
                это профиль канала.
  manual_only — тема требует глаз редактора:
                • федеральная и мировая политика, выборы, партии, протесты;
                • военное: БПЛА и тревоги, ПВО, обстрелы, мобилизация, СВО,
                  Украина, военкоматы, гибель военных;
                • непроверенные сведения: слухи, «очевидцы сообщают»,
                  анонимные паблики, отсутствие официального подтверждения;
                • суицид (сам факт), тяжёлые происшествия с детьми;
                • межнациональные и религиозные конфликты, мигранты;
                • обвинения конкретных людей и компаний без решения суда;
                • всё, что похоже на панику: «город без воды навсегда»,
                  «эвакуируют район», если это не подтверждено ведомством.
  reject      — публиковать нельзя:
                • упоминание запрещённых в РФ организаций и их символики;
                • способ совершения самоубийства, подробности расчленения,
                  натуралистические описания трупов и увечий;
                • сцены и подробности насилия над детьми;
                • призывы к насилию, вражде, погромам;
                • призыв выйти на несогласованную акцию и её оргдетали
                  (время, место, сбор) — независимо от повода;
                • инструкции по наркотикам, оружию, взрывчатке;
                • дискредитация армии, распространение заведомо ложных
                  сведений о действиях госорганов;
                • откровенная порнография, мат в тексте поста.

Правила:
  * оценивай текст, а не тему: «водитель погиб» — safe, «водителю оторвало
    голову» — reject;
  * сомневаешься между safe и manual_only — выбирай manual_only;
  * категории пиши по-русски, короткими ярлыками («военное», «слухи»)."""


@dataclass(frozen=True)
class SafetyResult:
    verdict: str
    categories: list[str] = field(default_factory=list)
    reason: str | None = None
    by: str = "rules"                    # rules | llm | fallback | disabled
    markers: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    error: str | None = None

    @property
    def blocks_autopublish(self) -> bool:
        return self.verdict != SAFE


class SafetyChecker:
    def __init__(self, client, settings: SafetySettings | None = None):
        self.client = client
        self.settings = settings or SafetySettings()

    def check(self, text: str, category: str | None = None) -> SafetyResult:
        by_rules = rules.check_text(text)
        # `reject` словаря не обсуждается и не стоит ни одного токена.
        if by_rules.verdict == REJECT:
            return SafetyResult(REJECT, by_rules.categories, "стоп-словарь",
                                by="rules", markers=by_rules.markers)
        if not self.settings.use_llm:
            return SafetyResult(by_rules.verdict, by_rules.categories,
                                "стоп-словарь" if by_rules.hits else None,
                                by="rules" if by_rules.hits else "disabled",
                                markers=by_rules.markers)

        body = (text or "")[:self.settings.max_chars]
        try:
            data, reply = self.client.ask_json(
                system=SYSTEM, user=f"Текст поста:\n\n{body}\n\nВердикт:",
                max_tokens=self.settings.max_tokens, purpose="safety")
        except LlmUnavailable:
            raise                                 # модель недоступна всем — проход прекращается
        except LlmError as exc:
            log.warning("safety-классификатор недоступен (%s) — драфт в ручную", exc)
            return SafetyResult(worst(by_rules.verdict, MANUAL_ONLY), by_rules.categories,
                                "классификатор недоступен", by="fallback",
                                markers=by_rules.markers, error=str(exc))

        verdict = str(data.get("verdict", "")).strip().lower()
        cost = getattr(reply, "cost_usd", 0.0)
        categories = [str(c) for c in (data.get("categories") or []) if c]
        reason = data.get("reason") or None
        if verdict not in VERDICTS:
            log.warning("непонятный вердикт safety %r — драфт в ручную", data.get("verdict"))
            return SafetyResult(worst(by_rules.verdict, MANUAL_ONLY),
                                _merge(by_rules.categories, categories),
                                "непонятный ответ классификатора", by="fallback",
                                markers=by_rules.markers, cost_usd=cost)

        final = worst(by_rules.verdict, verdict)
        return SafetyResult(final, _merge(by_rules.categories, categories), reason,
                            by="rules" if final == by_rules.verdict and by_rules.hits else "llm",
                            markers=by_rules.markers, cost_usd=cost)


def _merge(*lists) -> list[str]:
    out: list[str] = []
    for items in lists:
        for item in items:
            if item not in out:
                out.append(item)
    return out
