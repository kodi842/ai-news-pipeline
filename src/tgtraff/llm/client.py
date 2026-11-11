"""Клиент Haiku API: один вход для relevance, rewriter, safety и vision.

Зачем обёртка, а не anthropic.Anthropic() прямо в модулях:
  * деньги. Спека даёт фазе 4 $1-3 на калибровку и $1.2-2/день в проде —
    значит, каждый запрос должен считаться, а процесс уметь остановиться;
  * prompt caching. Стайлгайд и few-shot одинаковы у всех запросов рерайта,
    и ставить `cache_control` руками в трёх местах — способ однажды забыть;
  * тесты. Транспорт подставляется, и пайплайн проверяется без единого
    живого запроса (и без ключа в CI).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from tgtraff.llm.settings import CACHE_READ_RATE, CACHE_WRITE_RATE, LlmSettings

log = logging.getLogger("tgtraff.llm")


class LlmError(RuntimeError):
    """Ответ не получен или не разобран. Пайплайн переводит драфт в ручной режим."""


class LlmUnavailable(LlmError):
    """Модель недоступна ВСЕМ, а не этому кластеру: ключ не принят, гео-блок,
    нет сети, авария или лимит на стороне модели.

    Отличать это от сбоя одного запроса пришлось после живого прогона на
    российском VPS: `api.anthropic.com` отвечает Cloudflare-403 «Request not
    allowed», а пайплайн честно перебирал следующие сорок кластеров. Под
    таймером это одиннадцать тысяч заведомо мёртвых запросов в сутки.
    Наследование от LlmError даёт нужное поведение даром: проход и так
    останавливается по нему."""


class NoFacts(LlmError):
    """Материала не хватает на пост, и модель это сказала явно.

    Отличать от сбоя пришлось после прогона на сервере: кластер из одного
    заголовка возвращался в очередь и перебирался каждые пять минут. Очередь
    идёт от старых к новым — застрявшие кластеры занимали начало окна, и
    свежие новости до разбора не доходили. Решение по кластеру записывается
    один раз; вернётся он по §28, когда придёт фактура."""


# Коды, после которых следующий кластер не имеет смысла: дело не в запросе.
_GLOBAL_STATUSES = frozenset({401, 403, 404, 408, 409, 429})

# 400 обычно про КОНКРЕТНЫЙ запрос, но не всегда: пустой баланс тоже приходит
# четырёхсоткой, и он про ВСЁ. Поймано живьём 09.09 — конвейер драфтов
# перебирал кластеры на мёртвом балансе и валился трейсбеком вместо внятной
# строки «деньги кончились» в логе.
_GLOBAL_400_MARKERS = ("credit balance is too low", "billing", "insufficient")


def classify_transport_error(exc: Exception) -> Exception:
    """Ошибка транспорта → «стоп всему проходу» или «сбой одного кластера».

    400/422 остаются локальными: это про КОНКРЕТНЫЙ запрос (кластер-переросток,
    неразобранный промпт) — следующий кластер пройдёт нормально."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int) and (status in _GLOBAL_STATUSES or status >= 500):
        return LlmUnavailable(f"модель недоступна (HTTP {status}): {exc}")
    if status == 400 and any(m in str(exc).lower() for m in _GLOBAL_400_MARKERS):
        return LlmUnavailable(
            f"на счёте Anthropic кончился БАЛАНС — до пополнения конвейер драфтов "
            f"и завод клипов работать не могут: {exc}")
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return LlmUnavailable(f"модель недоступна (нет связи): {exc}")
    # Ошибки соединения SDK приходят своим классом — узнаём по имени, чтобы не
    # тянуть anthropic в импорт модуля ради одного isinstance.
    if type(exc).__name__ in ("APIConnectionError", "APITimeoutError"):
        return LlmUnavailable(f"модель недоступна (нет связи): {exc}")
    return exc


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0

    def add(self, other: "Usage") -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.cost_usd += other.cost_usd

    def line(self) -> str:
        return (f"запросов {self.calls}, вход {self.input_tokens}, "
                f"кэш(чтение/запись) {self.cache_read}/{self.cache_write}, "
                f"выход {self.output_tokens}, ${self.cost_usd:.4f}")


@dataclass(frozen=True)
class LlmReply:
    text: str
    model: str
    usage: Usage
    stop_reason: str | None = None

    @property
    def cost_usd(self) -> float:
        return self.usage.cost_usd


def cost_of(price_in: float, price_out: float, u) -> float:
    """$ за один запрос. Кэш дешевле входа в 10 раз, запись — дороже в 1.25."""
    inp = getattr(u, "input_tokens", 0) or 0
    out = getattr(u, "output_tokens", 0) or 0
    cread = getattr(u, "cache_read_input_tokens", 0) or 0
    cwrite = getattr(u, "cache_creation_input_tokens", 0) or 0
    return (inp * price_in + cread * price_in * CACHE_READ_RATE
            + cwrite * price_in * CACHE_WRITE_RATE + out * price_out) / 1_000_000


_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)


def parse_json_reply(text: str) -> dict:
    """JSON из ответа модели. Модель иногда обрамляет его текстом или ```json."""
    raw = (text or "").strip()
    fence = _FENCE_RE.search(raw)
    if fence:
        raw = fence.group(1).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise LlmError(f"ответ модели не содержит JSON: {text[:200]!r}")
        try:
            value = json.loads(raw[start:end + 1])
        except json.JSONDecodeError as exc:
            raise LlmError(f"ответ модели не разобрался как JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LlmError(f"ожидался объект JSON, пришло {type(value).__name__}")
    return value


class LlmClient:
    """Синхронный клиент. Транспорт — `messages.create` живого SDK или заглушка."""

    def __init__(self, settings: LlmSettings, transport=None, api_key: str | None = None):
        self.settings = settings
        self.total = Usage()
        self.by_purpose: dict[str, Usage] = {}
        # Кто-нибудь снаружи (конвейер) хочет записать каждый запрос в базу —
        # ставит сюда функцию (purpose, model, usage). Клиент про базу не знает.
        self.on_usage = None
        self._transport = transport or self._build_transport(api_key)

    def _build_transport(self, api_key: str | None):
        key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise LlmError(
                "Нет ANTHROPIC_API_KEY. Положите ключ в .env (шаблон — .env.example): "
                "фаза 4 без него работает только в режиме --dry-run."
            )
        import anthropic

        client = anthropic.Anthropic(
            api_key=key,
            timeout=self.settings.timeout_sec,
            max_retries=self.settings.max_retries,
        )
        return client.messages.create

    # ---------- запрос ----------

    def ask(self, system, user, max_tokens: int | None = None, purpose: str = "other",
            model: str | None = None, **ignored) -> LlmReply:
        """Один запрос. `system` кэшируется, `user` — нет (он меняется всегда)."""
        self._check_budget()
        model = model or self.settings.model
        kwargs = {
            "model": model,
            "max_tokens": max_tokens or self.settings.max_tokens,
            "system": self._system_blocks(system),
            "messages": [{"role": "user", "content": user}],
        }
        # temperature/top_p у текущих моделей Anthropic из API убраны (SDK 1.x
        # такой аргумент не принимает): детерминизм держим промптом.
        try:
            message = self._transport(**kwargs)
        except Exception as exc:                  # noqa: BLE001 — классифицируем и пробрасываем
            raise classify_transport_error(exc) from exc
        usage = self._account(message, model, purpose)
        text = "".join(b.text for b in getattr(message, "content", [])
                       if getattr(b, "type", None) == "text").strip()
        if not text:
            raise LlmError(f"пустой ответ модели (stop_reason={getattr(message, 'stop_reason', None)})")
        return LlmReply(text=text, model=getattr(message, "model", model), usage=usage,
                        stop_reason=getattr(message, "stop_reason", None))

    def ask_json(self, system, user, **kw) -> tuple[dict, LlmReply]:
        reply = self.ask(system, user, **kw)
        return parse_json_reply(reply.text), reply

    # ---------- служебное ----------

    def _system_blocks(self, system) -> list[dict]:
        """Системный промпт одним блоком с меткой кэша (спека §5).

        Осторожно: у Haiku 4.5 минимальный кэшируемый префикс — 4096 токенов.
        Промпт короче не закэшируется, и API об этом не скажет: проверять
        по `usage.cache_read_input_tokens` (см. отчёт фазы в PROGRESS)."""
        blocks = system if isinstance(system, list) else [{"type": "text", "text": system}]
        out = [dict(b) for b in blocks]
        if self.settings.cache_system and out:
            out[-1]["cache_control"] = {"type": "ephemeral"}
        return out

    def _account(self, message, model: str, purpose: str) -> Usage:
        raw = getattr(message, "usage", None)
        price_in, price_out = self.settings.price(model)
        usage = Usage(
            calls=1,
            input_tokens=getattr(raw, "input_tokens", 0) or 0,
            output_tokens=getattr(raw, "output_tokens", 0) or 0,
            cache_read=getattr(raw, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(raw, "cache_creation_input_tokens", 0) or 0,
            cost_usd=cost_of(price_in, price_out, raw) if raw else 0.0,
        )
        self.total.add(usage)
        self.by_purpose.setdefault(purpose, Usage()).add(usage)
        if self.on_usage is not None:
            self.on_usage(purpose, model, usage)
        return usage

    def _check_budget(self) -> None:
        limit = self.settings.budget_usd
        if limit and self.total.cost_usd >= limit:
            raise LlmError(
                f"исчерпан бюджет запросов: потрачено ${self.total.cost_usd:.4f} "
                f"при лимите ${limit:.2f}"
            )

    def report(self) -> str:
        lines = [f"итого: {self.total.line()}"]
        for purpose, usage in sorted(self.by_purpose.items()):
            lines.append(f"  {purpose:<10} {usage.line()}")
        return "\n".join(lines)


@dataclass
class NullClient:
    """Заглушка для `--dry-run`: правила работают, LLM не вызывается."""

    total: Usage = field(default_factory=Usage)
    by_purpose: dict = field(default_factory=dict)

    def ask(self, *a, **kw) -> LlmReply:
        raise LlmError("LLM отключён (--dry-run)")

    def ask_json(self, *a, **kw):
        raise LlmError("LLM отключён (--dry-run)")

    def report(self) -> str:
        return "LLM не вызывался"
