"""Обвязка над Haiku API: учёт токенов и денег, prompt caching, разбор ответа.

Живых запросов здесь нет — транспорт подставной. Проверяем то, что должно
работать одинаково при любом настроении модели: сколько потрачено, что
кэшируется и как разбирается ответ (спека §5: «Обязательно prompt caching»).
"""
import pytest

from tgtraff.llm.client import LlmClient, LlmError, parse_json_reply
from tgtraff.llm.settings import LlmSettings


class FakeUsage:
    def __init__(self, inp=100, out=50, cread=0, cwrite=0):
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_read_input_tokens = cread
        self.cache_creation_input_tokens = cwrite


class FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeMessage:
    def __init__(self, text, usage=None, stop_reason="end_turn"):
        self.content = [FakeBlock(text)]
        self.usage = usage or FakeUsage()
        self.stop_reason = stop_reason
        self.model = "claude-haiku-4-5"


class FakeTransport:
    """Заменяет client.messages.create — запоминает, что ему передали."""

    def __init__(self, replies=None):
        self.replies = list(replies or ["ответ"])
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, FakeMessage) else FakeMessage(reply)


def client(transport, **kw) -> LlmClient:
    return LlmClient(LlmSettings(**kw), transport=transport)


def test_reply_text_comes_back_from_the_model():
    c = client(FakeTransport(["Заголовок"]))
    assert c.ask(system="s", user="u").text == "Заголовок"


def test_model_and_max_tokens_go_to_the_api():
    t = FakeTransport()
    client(t, model="claude-haiku-4-5").ask(system="s", user="u", max_tokens=333)
    assert t.calls[0]["model"] == "claude-haiku-4-5"
    assert t.calls[0]["max_tokens"] == 333


def test_system_prompt_is_marked_for_caching():
    """Стайлгайд и few-shot одни и те же на каждом запросе — они обязаны кэшироваться."""
    t = FakeTransport()
    client(t).ask(system="большой стайлгайд", user="u")
    system = t.calls[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[0]["text"] == "большой стайлгайд"


def test_volatile_user_text_is_not_cached():
    t = FakeTransport()
    client(t).ask(system="s", user="текст кластера")
    assert t.calls[0]["messages"] == [{"role": "user", "content": "текст кластера"}]


def test_cost_counts_input_output_and_cache_rates():
    # haiku 4.5: вход $1, выход $5 за 1M; запись кэша ×1.25, чтение ×0.1.
    usage = FakeUsage(inp=1_000_000, out=1_000_000, cread=1_000_000, cwrite=1_000_000)
    c = client(FakeTransport([FakeMessage("ok", usage)]))
    reply = c.ask(system="s", user="u")
    assert reply.cost_usd == pytest.approx(1.0 + 5.0 + 0.1 + 1.25)


def test_client_totals_accumulate_across_calls():
    c = client(FakeTransport(["раз", "два"]))
    c.ask(system="s", user="u")
    c.ask(system="s", user="u")
    assert c.total.calls == 2
    assert c.total.cost_usd > 0
    assert c.total.input_tokens == 200


def test_purpose_is_kept_in_usage_breakdown():
    c = client(FakeTransport(["a", "b"]))
    c.ask(system="s", user="u", purpose="rewrite")
    c.ask(system="s", user="u", purpose="safety")
    assert set(c.by_purpose) == {"rewrite", "safety"}


def test_budget_limit_stops_the_client():
    """Калибровка фазы 4 стоит $1-3 (спека). Предохранитель обязан быть в коде."""
    usage = FakeUsage(inp=1_000_000, out=1_000_000)
    c = client(FakeTransport([FakeMessage("ok", usage)]), budget_usd=1.0)
    c.ask(system="s", user="u")            # уже $6 — бюджет исчерпан
    with pytest.raises(LlmError, match="бюджет"):
        c.ask(system="s", user="u")


def test_empty_reply_raises_rather_than_returning_nothing():
    msg = FakeMessage("")
    msg.content = []
    with pytest.raises(LlmError):
        client(FakeTransport([msg])).ask(system="s", user="u")


# --- разбор JSON-ответа ----------------------------------------------------

def test_plain_json_is_parsed():
    assert parse_json_reply('{"category": "dtp"}') == {"category": "dtp"}


def test_json_in_a_code_fence_is_parsed():
    assert parse_json_reply('```json\n{"a": 1}\n```') == {"a": 1}


def test_json_with_chatter_around_it_is_parsed():
    assert parse_json_reply('Вот ответ:\n{"a": 1}\nГотово.') == {"a": 1}


def test_broken_json_raises_llm_error():
    with pytest.raises(LlmError):
        parse_json_reply("не json вовсе")


# ---- «модель недоступна всем» против «сбой одного кластера» ----
#
# Живой прогон на РФ-сервере: api.anthropic.com отвечает Cloudflare-403
# «Request not allowed» (гео-блок). Пайплайн считал это сбоем ОДНОГО кластера
# и честно перебирал следующие сорок — под таймером это одиннадцать тысяч
# заведомо мёртвых запросов в сутки. Такие ответы должны останавливать проход
# целиком, а разбирать их — уже человеку.

class ApiError(Exception):
    """Двойник ошибки SDK: у неё есть status_code, как у anthropic.APIStatusError."""

    def __init__(self, status_code, message="ошибка"):
        super().__init__(message)
        self.status_code = status_code


def client_raising(exc, **kw):
    return LlmClient(LlmSettings(**kw), api_key="k", transport=FakeTransport([exc]))


@pytest.mark.parametrize("status,why", [
    (401, "ключ не принят"),
    (403, "гео-блок или нет доступа"),
    (404, "модели с таким именем нет"),
    (429, "лимит запросов"),
    (500, "у модели авария"),
    (529, "модель перегружена"),
])
def test_global_failures_stop_the_whole_pass(status, why):
    from tgtraff.llm.client import LlmUnavailable

    with pytest.raises(LlmUnavailable) as e:
        client_raising(ApiError(status)).ask("s", "u")
    assert str(status) in str(e.value), f"в тексте нужен код ответа ({why})"


def test_no_network_stops_the_whole_pass():
    from tgtraff.llm.client import LlmUnavailable

    with pytest.raises(LlmUnavailable):
        client_raising(ConnectionError("нет сети")).ask("s", "u")


def test_a_bad_request_stays_a_single_cluster_failure():
    """400 — это про КОНКРЕТНЫЙ запрос (кластер-переросток, кривой промпт).
    Останавливать из-за него весь проход нельзя: следующий кластер пройдёт."""
    from tgtraff.llm.client import LlmUnavailable

    with pytest.raises(ApiError):
        client_raising(ApiError(400)).ask("s", "u")
    try:
        client_raising(ApiError(400)).ask("s", "u")
    except Exception as exc:
        assert not isinstance(exc, LlmUnavailable)


def test_empty_balance_stops_the_whole_pass():
    """400 обычно про конкретный запрос, но «credit balance is too low» — про
    ВСЁ: следующий кластер получит тот же ответ. Поймано живьём 09.09 —
    конвейер драфтов перебирал кластеры на мёртвом балансе и валился
    трейсбеком вместо внятной строки в логе."""
    from tgtraff.llm.client import LlmUnavailable

    exc = ApiError(400, "Your credit balance is too low to access the Anthropic API. "
                        "Please go to Plans & Billing to upgrade or purchase credits.")
    with pytest.raises(LlmUnavailable) as e:
        client_raising(exc).ask("s", "u")
    assert "баланс" in str(e.value).lower()


def test_ordinary_bad_request_is_still_a_single_cluster_failure():
    """Кластер-переросток и кривой промпт по-прежнему не роняют проход."""
    from tgtraff.llm.client import LlmUnavailable

    exc = ApiError(400, "prompt is too long: 250000 tokens > 200000 maximum")
    try:
        client_raising(exc).ask("s", "u")
    except Exception as raised:
        assert not isinstance(raised, LlmUnavailable)


def test_unavailable_is_an_llm_error_so_the_pipeline_already_breaks():
    """Пайплайн останавливает проход по LlmError — наследование даёт это даром."""
    from tgtraff.llm.client import LlmUnavailable

    assert issubclass(LlmUnavailable, LlmError)
