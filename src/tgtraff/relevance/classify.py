"""Relevance-фильтр целиком: правила, затем дешёвый классификатор (спека §4).

Порядок неслучаен: правила бесплатны и детерминированы, поэтому реклама с erid
и гороскопы отсеиваются до единого платного токена (требование спеки §5 —
«правила до LLM»). Классификатору достаётся то, что правилами не решается:
в первую очередь «нерелевантные городу» посты — их не различить по словарю.

Отказ модели новость не убивает: непонятный ответ и упавший API означают
`news`, то есть кластер идёт дальше в конвейер. Цена ошибки несимметрична —
лишний драфт модератор закроет кликом, а выброшенная новость не вернётся.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from tgtraff.llm.client import LlmError, LlmUnavailable
from tgtraff.relevance import rules
from tgtraff.relevance.settings import RelevanceSettings

log = logging.getLogger("tgtraff.relevance")

NEWS = "news"
VERDICTS = (NEWS, rules.AD, rules.CONTEST, rules.HOROSCOPE, "off_topic")

SYSTEM = """Ты фильтр городской новостной ленты. На вход — публикации одной новости
из городских источников. Ответь, годится ли эта новость для городского
Telegram-канала «ЧП [город]» (происшествия, ДТП, пожары, коммуналка, погода,
городская повестка, криминал).

Верни ТОЛЬКО JSON: {"verdict": "<вердикт>", "reason": "<коротко, по-русски>"}

Вердикты:
  news      — городская новость или происшествие, касающееся жителей этого города
  ad        — рекламный пост, промо услуги/товара/заведения, приглашение на курс
  contest   — розыгрыш, конкурс с подпиской/репостом, раздача призов
  horoscope — гороскоп, астрология, эзотерика, «что говорят звёзды»
  off_topic — событие другого города или региона без связи с нашим, федеральная
              и мировая повестка, шоу-бизнес, «интересные факты», опрос без
              фактуры, объявление о продаже/покупке, вакансия, поздравление
              с праздником, реклама самого источника

Важно:
  * событие в области или у соседей — off_topic, если в тексте нет привязки к
    нашему городу (жители, предприятия, дороги, ведомства этого города);
  * новость о конкурсе или соревновании, где участвовали земляки, — это news,
    а не contest: contest — только розыгрыш с механикой подписки;
  * сомневаешься между news и off_topic — выбирай news."""


@dataclass(frozen=True)
class RelevanceResult:
    verdict: str
    reason: str | None = None
    by: str = "rules"                 # rules | llm | fallback | disabled
    cost_usd: float = 0.0
    error: str | None = None

    @property
    def is_drop(self) -> bool:
        return self.verdict != NEWS


class RelevanceChecker:
    def __init__(self, client, settings: RelevanceSettings | None = None):
        self.client = client
        self.settings = settings or RelevanceSettings()

    def check(self, material) -> RelevanceResult:
        verdict = self._by_rules(material)
        if verdict is not None:
            return verdict
        if not self.settings.use_llm:
            return RelevanceResult(NEWS, by="disabled")
        return self._by_llm(material)

    # ---------- правила ----------

    def _by_rules(self, material) -> RelevanceResult | None:
        """Кластер отсеивается, только если правила уверены по ВСЕМ его постам.

        Смешанный кластер («реклама» приклеилась к новости) — повод спросить
        классификатор, а не выбросить новость по одному участнику."""
        verdicts = [rules.check_post(p.raw_text, marked_as_ads=p.marked_as_ads)
                    for p in material.posts]
        if not verdicts or not all(v.is_drop for v in verdicts):
            return None
        first = verdicts[0]
        return RelevanceResult(first.reason, first.marker, by="rules")

    # ---------- классификатор ----------

    def _by_llm(self, material) -> RelevanceResult:
        prompt = self._prompt(material)
        try:
            data, reply = self.client.ask_json(
                system=SYSTEM, user=prompt, max_tokens=self.settings.max_tokens,
                purpose="relevance")
        except LlmUnavailable:
            raise                                 # модель недоступна всем — проход прекращается
        except LlmError as exc:
            log.warning("кластер #%s: классификатор недоступен (%s) — оставляем в ленте",
                        material.cluster_id, exc)
            return RelevanceResult(NEWS, by="fallback", error=str(exc))

        verdict = str(data.get("verdict", "")).strip().lower()
        reason = (data.get("reason") or None)
        cost = getattr(reply, "cost_usd", 0.0)
        if verdict not in VERDICTS:
            log.warning("кластер #%s: непонятный вердикт %r — оставляем в ленте",
                        material.cluster_id, data.get("verdict"))
            return RelevanceResult(NEWS, by="fallback", cost_usd=cost,
                                   error=f"вердикт {data.get('verdict')!r}")
        if verdict == "off_topic" and not self.settings.drop_off_topic:
            return RelevanceResult(NEWS, reason, by="llm", cost_usd=cost)
        return RelevanceResult(verdict, reason, by="llm", cost_usd=cost)

    def _prompt(self, material) -> str:
        body = material.as_prompt()
        limit = self.settings.max_chars
        if len(body) > limit:
            body = body[:limit] + " …"
        return f"{body}\n\nВердикт по этой новости для канала «ЧП {material.city_name}»:"
