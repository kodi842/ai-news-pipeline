"""Правила relevance-фильтра (спека §4): что отсеивается без обращения к LLM.

Спека: «Отсев: реклама источников (маркеры „Реклама“, erid, промо-ссылки),
чужие конкурсы, гороскопы, нерелевантные городу посты. Правила + дешёвый
LLM-классификатор». Здесь — первая половина: дешёвая, детерминированная и
бесплатная. Всё, в чём правила не уверены, уходит классификатору (`unsure`).

Принцип: правило имеет право ронять пост только по однозначному маркеру.
«Конкурс» в тексте — ещё не конкурс канала (живой пример: «конкурс баянистов
Кубок мира», где магнитогорцы взяли серебро — это новость города). Поэтому
конкурс роняется только вместе с механикой розыгрыша (подписаться/репост/приз).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Категории отсева. `news` — пост остаётся в конвейере.
AD = "ad"
CONTEST = "contest"
HOROSCOPE = "horoscope"


@dataclass(frozen=True)
class RuleVerdict:
    """`drop` — правило уверено; `unsure` — решает классификатор."""

    verdict: str                    # drop | unsure
    reason: str | None = None       # ad | contest | horoscope
    marker: str | None = None       # что именно сработало (для отчёта и разбора)

    @property
    def is_drop(self) -> bool:
        return self.verdict == "drop"


UNSURE = RuleVerdict("unsure")

# --- реклама ---------------------------------------------------------------

# Единый идентификатор рекламы (маркировка ОРД). Пишут по-разному: erid, ERID,
# «Ерид», через двоеточие или пробел. Ложных срабатываний у него не бывает.
_ERID_RE = re.compile(r"\b(erid|ерид)\b", re.I | re.U)

# Хэштег и служебные подписи рекламных постов ВК/ТГ.
_AD_MARKERS = (
    "#реклама", "#промо", "#партнёрскийматериал", "#партнерскийматериал",
    "о рекламодателе", "на правах рекламы", "рекламное объявление",
)
# Ярлык маркировки — именительный падеж отдельным словом («Реклама. ООО …»).
# Падежные формы к делу не относятся: «УФАС остановило рекламу дезинфекции» —
# это новость про рекламу, а не рекламный пост (живой пример, лента 01.09).
_AD_LABEL_RE = re.compile(r"\bреклама\b", re.I | re.U)
# Реквизиты рекламодателя рядом с ярлыком. Границы слова обязательны: без них
# «ип» находится внутри «тип», а «ао» — внутри «маош».
_ADVERTISER_DETAILS_RE = re.compile(
    r"\b(инн|огрн|огрнип|ип|ооо|ао|зао|пао|токен|самозанят\w*)\b", re.I | re.U
)
# Ярлык и реквизиты стоят рядом («Реклама. ООО «СибМоторс». ИНН 4217125362»),
# в каком порядке — зависит от источника, поэтому окно двустороннее.
_DISCLOSURE_WINDOW = 120


def _looks_like_ad(low: str) -> str | None:
    if _ERID_RE.search(low):
        return "erid"
    for marker in _AD_MARKERS:
        if marker in low:
            return marker
    for m in _AD_LABEL_RE.finditer(low):
        window = low[max(0, m.start() - _DISCLOSURE_WINDOW):m.end() + _DISCLOSURE_WINDOW]
        if _ADVERTISER_DETAILS_RE.search(window):
            return "реклама + реквизиты"
    return None


# --- чужие конкурсы --------------------------------------------------------

_GIVEAWAY_WORDS = ("розыгрыш", "разыгрываем", "разыгрывае", "конкурс", "giveaway")
# Механика розыгрыша: без неё «конкурс» — обычная новость о состязании.
_GIVEAWAY_MECHANICS = (
    "подписаться на канал", "подпишись", "подпишитесь", "подписывайтесь",
    "сделать репост", "сделай репост", "репост", "поставить лайк", "поставь лайк",
    "отметь друга", "отметить друга", "участвуй", "участвуйте", "условия участия",
    "победителя определим", "победителя выберем", "разыграем", "приз получит",
    "случайным образом", "рандом",
)


def _looks_like_giveaway(low: str) -> str | None:
    if not any(w in low for w in _GIVEAWAY_WORDS):
        return None
    for mech in _GIVEAWAY_MECHANICS:
        if mech in low:
            return mech
    return None


# --- гороскопы -------------------------------------------------------------

# Границы слова обязательны: «таро» без них находится внутри «старой» и
# «старого» — на живой ленте это давало гороскоп из новости про роддом.
_HOROSCOPE_RE = re.compile(
    r"\b(гороскоп\w*|астропрогноз\w*|астролог\w*|зодиак\w*|нумеролог\w*|"
    r"таро|таролог\w*|лунный календарь|ретроградный меркурий)\b",
    re.I | re.U,
)


def _looks_like_horoscope(low: str) -> str | None:
    m = _HOROSCOPE_RE.search(low)
    return m.group(0) if m else None


def check_post(text: str | None, marked_as_ads: bool = False) -> RuleVerdict:
    """Вердикт правил по одному посту.

    `marked_as_ads` — готовый флаг ВК API (собирается коллектором фазы 2):
    площадка сама сказала, что это рекламная запись, спорить не с чем.
    """
    if marked_as_ads:
        return RuleVerdict("drop", AD, "marked_as_ads")

    low = (text or "").lower().replace("ё", "е")
    if not low.strip():
        return UNSURE

    marker = _looks_like_ad(low)
    if marker:
        return RuleVerdict("drop", AD, marker)
    marker = _looks_like_giveaway(low)
    if marker:
        return RuleVerdict("drop", CONTEST, marker)
    marker = _looks_like_horoscope(low)
    if marker:
        return RuleVerdict("drop", HOROSCOPE, marker)
    return UNSURE
