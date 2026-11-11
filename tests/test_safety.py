"""Safety-фильтр (спека §6): стоп-словарь + классификатор → safe/manual_only/reject.

Правило комбинации: берётся ХУДШИЙ из двух вердиктов. Стоп-словарь ловит то,
что нельзя пропустить ни при каком настроении модели; классификатор — то, что
словарём не описать.

Автопубликация в фазе 4 не включается ни при каком вердикте (спека: «safe-автопубликация
выключена по умолчанию; включается вручную по категориям после калибровки»).
"""
import pytest

from tgtraff.llm.client import LlmError
from tgtraff.safety.checker import SafetyChecker
from tgtraff.safety.rules import MANUAL_ONLY, REJECT, SAFE, check_text
from tgtraff.safety.settings import SafetySettings


class StubClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def ask_json(self, system, user, **kw):
        self.prompts.append(user)
        reply = self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply

        class _R:
            model = "stub"
            cost_usd = 0.0003
        return reply, _R()


# --- стоп-словарь: reject ---------------------------------------------------

def test_banned_organisation_is_rejected():
    v = check_text("В городе задержали сторонника ИГИЛ, признанного террористической организацией")
    assert v.verdict == REJECT
    assert "запрещённые организации" in v.categories


def test_suicide_method_details_are_rejected():
    v = check_text("Подросток покончил с собой, шагнув с крыши девятиэтажки: "
                   "он оставил записку и спрыгнул с парапета на глазах у прохожих")
    assert v.verdict == REJECT


def test_graphic_gore_is_rejected():
    v = check_text("Тело мужчины нашли расчленённым, части трупа лежали в мешке у гаражей")
    assert v.verdict == REJECT


# --- стоп-словарь: manual_only ----------------------------------------------

def test_drone_alert_goes_to_manual():
    v = check_text("В Череповце и округе введён режим «Беспилотная опасность». "
                   "Жителей просят не снимать работу ПВО")
    assert v.verdict == MANUAL_ONLY
    assert "военное" in v.categories


def test_federal_politics_goes_to_manual():
    v = check_text("Владимир Путин подписал указ о новых выплатах; в Госдуме обсуждают "
                   "поправки к бюджету")
    assert v.verdict == MANUAL_ONLY
    assert "федеральная политика" in v.categories


def test_fake_markers_go_to_manual():
    v = check_text("По слухам, в городе закрывают комбинат — пишут в анонимных пабликах, "
                   "официального подтверждения нет")
    assert v.verdict == MANUAL_ONLY
    assert "маркеры фейков" in v.categories


# --- обычное ЧП остаётся публикуемым ----------------------------------------

def test_ordinary_fatal_crash_is_not_blocked():
    """ЧП-канал пишет о погибших в ДТП — иначе он не ЧП-канал."""
    v = check_text("На трассе под Магнитогорском столкнулись «Лада» и грузовик, "
                   "водитель легковушки погиб на месте, пассажира увезли в больницу")
    assert v.verdict == SAFE


def test_ordinary_fire_is_safe():
    assert check_text("Горел расселённый дом на Наседкина, пострадавших нет").verdict == SAFE


def test_local_authorities_are_not_federal_politics():
    v = check_text("Мэр Череповца пообещал закончить ремонт улицы Ленина к октябрю")
    assert v.verdict == SAFE


# --- комбинация с классификатором -------------------------------------------

def safety(client, **kw):
    return SafetyChecker(client, SafetySettings(**kw))


def test_classifier_can_downgrade_a_clean_looking_post():
    client = StubClient({"verdict": "manual_only", "categories": ["военное"],
                         "reason": "речь о работе ПВО"})
    result = safety(client).check("Ночью над городом что-то громко хлопало, "
                                  "жители сняли вспышки в небе")
    assert result.verdict == MANUAL_ONLY
    assert result.by == "llm"


def test_worst_verdict_wins():
    client = StubClient({"verdict": "safe", "categories": [], "reason": ""})
    result = safety(client).check("Задержан сторонник ИГИЛ")
    assert result.verdict == REJECT      # словарь строже модели


def test_rules_reject_does_not_pay_for_a_model_call():
    client = StubClient({"verdict": "safe"})
    safety(client).check("Задержан сторонник ИГИЛ")
    assert client.prompts == []


def test_model_failure_falls_back_to_manual_only():
    """Не смогли проверить — значит, руками. Молча публиковать нельзя."""
    result = safety(StubClient(LlmError("timeout"))).check("Обычная новость про ремонт дороги")
    assert result.verdict == MANUAL_ONLY
    assert result.by == "fallback"


def test_unknown_verdict_falls_back_to_manual_only():
    result = safety(StubClient({"verdict": "ok"})).check("Обычная новость про ремонт дороги")
    assert result.verdict == MANUAL_ONLY


def test_classifier_can_be_switched_off():
    client = StubClient({"verdict": "reject"})
    result = safety(client, use_llm=False).check("Ремонт дороги на Ленина закончат в октябре")
    assert result.verdict == SAFE
    assert client.prompts == []


# --- набор провокаций (приёмка фазы) ----------------------------------------

def test_provocation_set_exists_and_is_complete():
    from tgtraff.safety.probe import load_probes
    probes = load_probes()
    assert len(probes) >= 20
    assert {p.expected for p in probes} <= {SAFE, MANUAL_ONLY, REJECT}
    assert sum(1 for p in probes if p.expected == REJECT) >= 5
    assert sum(1 for p in probes if p.expected == SAFE) >= 4


@pytest.mark.parametrize("probe", __import__(
    "tgtraff.safety.probe", fromlist=["load_probes"]).load_probes(),
    ids=lambda p: p.id)
def test_stop_words_never_call_a_forbidden_post_safe(probe):
    """Недопустимое словарь обязан поднять сам, без модели.

    Различать reject и manual_only он при этом не обязан: дискредитацию армии
    и призыв к акции словами не описать (это смысл, а не лексика) — до reject
    их доводит классификатор. Обвинение без суда словарь не ловит вовсе, и это
    честно: «украл» в тексте бывает и в законной криминальной хронике.
    Итоговые вердикты по всем 20 провокациям — приёмка фазы,
    scripts/safety_probe.py."""
    if probe.expected != REJECT:
        pytest.skip("manual_only ловит классификатор: это смысл, а не слова")
    assert check_text(probe.text).verdict != SAFE


@pytest.mark.parametrize("probe", __import__(
    "tgtraff.safety.probe", fromlist=["load_probes"]).load_probes(),
    ids=lambda p: p.id)
def test_stop_words_do_not_block_safe_cases(probe):
    if probe.expected != SAFE:
        pytest.skip("не safe-кейс")
    assert check_text(probe.text).verdict == SAFE
