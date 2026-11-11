"""Сценарий клипа (VIDEO-SPEC): хук ≤6 слов, 2-3 строки фактов, CTA-концовка.

Первые 1.5 секунды решают, останется ли зритель, — поэтому хук короткий и
крупный. Дальше 2-3 строки фактов: в режиме видео они лежат субтитрами, в
режиме фото их ЧИТАЕТ озвучка (и субтитры = озвучка, слово в слово).

Факты: сценарий пишется по уже проверенному драфту, значит любое НОВОЕ число
или имя собственное — выдумка. Сверка та же, что у рерайта (`unsupported_facts`
из rewriter), только материалом служит сам драфт. Поймали выдумку — один раз
переспрашиваем, второй раз оставляем с флагом: клип не теряем, но человек в
карточке видит, что проверить надо глазами.

Юзернейм канала в CTA приходит из конфига, а не от модели: адрес нашего
собственного канала — не то, что можно доверить генерации.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from tgtraff.clips.settings import ClipSettings
from tgtraff.rewriter.rewrite import PROMPTS_DIR, unsupported_facts

log = logging.getLogger("tgtraff.clips.script")

# Хук — плашка на 1.5 секунды. Больше шести слов на ней не читается с телефона.
MAX_HOOK_WORDS = 6
MAX_LINES = 3
# Потолок строки субтитра. Плашка переносит текст сама (до трёх строк), так что
# это защита от простыни, а не от переноса.
MAX_LINE_CHARS = 120
DEFAULT_CTA = "Подробности — в нашем канале"

CLIP_RULES = """
# Дополнительно: это сценарий КЛИПА, а не поста

Пост по этой новости уже вышел и проверен. Твоя работа — упаковать его в
вертикальное видео на 15-40 секунд. Ничего нового к фактам поста добавлять
нельзя: ни числа, ни имени, ни улицы, ни причины, которых в нём нет.

  * `hook` — плашка первых 1.5 секунды: НЕ БОЛЬШЕ шести слов, без точки в
    конце, без кавычек и капслока. Суть события первым словом. Это не
    заголовок поста — короче и злее: «Автобус смял столб», «Горит рынок»,
    «Воду дадут в пятницу»;
  * `lines` — 2-3 строки фактов, каждая до 90 знаков, читается вслух за 3-4
    секунды. Порядок: что случилось → где и когда → последствия. Без
    вводных, без «как сообщает», без обращений к зрителю;
  * `cta` — короткая концовка-призыв БЕЗ адреса канала и без @ник: адрес
    подставит конвейер. Например «Подробности — в нашем канале».

Верни ТОЛЬКО JSON:
{"hook": "...", "lines": ["...", "..."], "cta": "..."}
"""


class ScriptError(RuntimeError):
    """Сценарий не собрался: пустой хук или ни одной строки фактов."""


# --- числа прописью -------------------------------------------------------
#
# Сверка рерайта смотрит ЦИФРЫ, а сценарий клипа произносится голосом, и
# модель пишет его так, как говорят: «пострадали три человека». Для сверки
# такое число невидимо — и выдуманные жертвы уехали бы на площадку.
#
# Считаем фактом только количество ЛЮДЕЙ И ОБЪЕКТОВ («три человека», «пять
# машин»): там и появляются придуманные числа. Нумерация («дом номер пять»,
# «двадцать первый маршрут») в счёт не идёт — иначе сверка ловила бы форму
# записи, как это уже было с датами в §33.
_NUMERALS = {
    2: ("два", "две", "двое", "двух", "двоих"),
    3: ("три", "трое", "трех", "троих"),
    4: ("четыре", "четверо", "четырех", "четверых"),
    5: ("пять", "пятеро", "пяти", "пятерых"),
    6: ("шесть", "шести", "шестеро"),
    7: ("семь", "семи", "семеро"),
    8: ("восемь", "восьми", "восьмеро"),
    9: ("девять", "девяти"),
    10: ("десять", "десяти"),
    11: ("одиннадцать", "одиннадцати"),
    12: ("двенадцать", "двенадцати"),
    13: ("тринадцать", "тринадцати"),
    14: ("четырнадцать", "четырнадцати"),
    15: ("пятнадцать", "пятнадцати"),
    16: ("шестнадцать", "шестнадцати"),
    17: ("семнадцать", "семнадцати"),
    18: ("восемнадцать", "восемнадцати"),
    19: ("девятнадцать", "девятнадцати"),
    20: ("двадцать", "двадцати"),
    30: ("тридцать", "тридцати"),
    40: ("сорок", "сорока"),
    50: ("пятьдесят", "пятидесяти"),
    60: ("шестьдесят", "шестидесяти"),
    70: ("семьдесят", "семидесяти"),
    80: ("восемьдесят", "восьмидесяти"),
    90: ("девяносто", "девяноста"),
    100: ("сто", "ста"),
    200: ("двести", "двухсот"),
    300: ("триста", "трехсот"),
    1000: ("тысяча", "тысячи", "тысяч", "тысячу"),
}
_WORD_TO_VALUE = {word: value for value, words in _NUMERALS.items() for word in words}

# После чего число прописью считается количеством, а не нумерацией.
_COUNT_NOUNS = (
    "человек", "людей", "пострадав", "погибш", "ранен", "жертв", "детей", "ребен",
    "школьник", "жител", "семей", "машин", "автомобил", "единиц", "квартир", "домов",
    "подъезд", "этаж", "минут", "часов", "суток", "дней", "недел", "метров", "километр",
)
_WORD_RE = re.compile(r"[\w-]+", re.U)


def numeral_counts(text: str) -> list[tuple[str, int]]:
    """Числа прописью, стоящие количеством: [(слово, значение)]."""
    words = _WORD_RE.findall((text or "").lower().replace("ё", "е"))
    out: list[tuple[str, int]] = []
    for i, word in enumerate(words):
        value = _WORD_TO_VALUE.get(word)
        if value is None:
            continue
        tail = " ".join(words[i + 1:i + 3])
        if any(noun in tail for noun in _COUNT_NOUNS):
            out.append((word, value))
    return out


def _known_values(source: str) -> set[int]:
    """Числа материала: и цифрами, и прописью."""
    digits = {int(m) for m in re.findall(r"\d+", source or "")}
    return digits | {value for _, value in numeral_counts(source)} | \
        {value for word, value in _WORD_TO_VALUE.items()
         if re.search(rf"\b{word}\b", (source or "").lower().replace("ё", "е"))}


def unsupported_clip_facts(clip_text: str, source: str) -> list[str]:
    """Что в сценарии есть, а в драфте нет: цифры, имена и числа прописью."""
    missing = list(unsupported_facts(clip_text, [source]))
    known = _known_values(source)
    for word, value in numeral_counts(clip_text):
        if value not in known and word not in missing:
            missing.append(word)
    return missing


@dataclass(frozen=True)
class ClipScript:
    hook: str
    lines: list[str]
    cta: str
    username: str | None = None
    flags: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    model: str = ""
    cost_usd: float = 0.0

    @property
    def cta_text(self) -> str:
        """Концовка вместе с адресом канала — так она уходит на аутро."""
        if not self.username:
            return self.cta
        return f"{self.cta}\n@{self.username}"

    @property
    def speech_text(self) -> str:
        """Что читает озвучка в режиме фото. Он же — источник субтитров:
        приёмка V1 требует, чтобы они совпадали, поэтому текст ОДИН."""
        return " ".join([self.hook.rstrip(".") + ".", *self.lines])

    @property
    def subtitle_lines(self) -> list[str]:
        return list(self.lines)


class ClipScriptWriter:
    def __init__(self, client, settings: ClipSettings | None = None,
                 max_hook_words: int = MAX_HOOK_WORDS, retries: int = 1):
        self.client = client
        self.settings = settings or ClipSettings()
        self.max_hook_words = max_hook_words
        self.retries = retries
        self._system = _load_system()

    @property
    def system_prompt(self) -> str:
        return self._system

    def write(self, draft, media, city_name: str | None = None,
              username: str | None = None, hint: str | None = None) -> ClipScript:
        """Сценарий по драфту. `hint` — просьба модератора «перегенерить хук»."""
        source = f"{_get(draft, 'title') or ''}\n{_get(draft, 'body') or ''}"
        attempts: list[ClipScript] = []
        spent = 0.0
        for attempt in range(self.retries + 1):
            data, reply = self.client.ask_json(
                system=self._system,
                user=self._prompt(draft, media, city_name, hint=hint, retry=attempt > 0),
                max_tokens=500, purpose="clip_script")
            spent += getattr(reply, "cost_usd", 0.0) or 0.0
            script = self._parse(data, source, username,
                                 model=getattr(reply, "model", ""), cost_usd=spent)
            if not script.flags:
                return script
            attempts.append(script)
        # Дважды не получилось — берём последнюю попытку с флагами: клип
        # уйдёт человеку, и он решит (у него есть кнопка «Перегенерить хук»).
        log.info("сценарий клипа с замечаниями: %s", ", ".join(attempts[-1].flags))
        return attempts[-1]

    # ---------- разбор ----------

    def _parse(self, data: dict, source: str, username: str | None,
               model: str, cost_usd: float) -> ClipScript:
        hook = _clean_hook(str(data.get("hook") or ""))
        if not hook:
            raise ScriptError("модель не дала хук")
        lines = [_clean_line(x) for x in (data.get("lines") or [])]
        lines = [x for x in lines if x][:MAX_LINES]
        if not lines:
            raise ScriptError("модель не дала ни одной строки фактов")
        cta = _clean_line(str(data.get("cta") or "")) or DEFAULT_CTA
        cta = _strip_handles(cta)

        flags: list[str] = []
        if len(hook.split()) > self.max_hook_words:
            flags.append("hook_long")
        unsupported = unsupported_clip_facts("\n".join([hook, *lines]), source)
        if unsupported:
            flags.append("unsupported_facts")
        return ClipScript(hook=hook, lines=lines, cta=cta, username=username,
                          flags=flags, unsupported=unsupported, model=model,
                          cost_usd=cost_usd)

    # ---------- промпт ----------

    def _prompt(self, draft, media, city_name: str | None, hint: str | None,
                retry: bool) -> str:
        mode = getattr(media, "mode", "video")
        if mode == "photos":
            how = (f"Режим сборки: слайдшоу из {len(getattr(media, 'files', []) or [])} фото. "
                   "Строки фактов ОЗВУЧИВАЕТ синтезированный голос, и они же лежат "
                   "субтитрами — пиши так, как говорят вслух.")
        else:
            seconds = getattr(media, "duration", None)
            how = ("Режим сборки: живое видео с места"
                   + (f" ({seconds:.0f} с)" if seconds else "")
                   + ". Строки фактов лежат субтитрами под кадром, звук — оригинальный.")
        lines = [f"Город: {city_name or _get(draft, 'city') or ''}",
                 f"Категория: {_get(draft, 'category') or 'other'}",
                 how, "",
                 "Пост, который уже вышел в канале:",
                 f"Заголовок: {_get(draft, 'title') or ''}",
                 f"Текст: {_get(draft, 'body') or ''}", ""]
        if hint:
            lines.append(f"Пожелание модератора: {hint}")
        if retry:
            lines.append("ПРЕДЫДУЩИЙ ОТВЕТ НЕ ПОДОШЁЛ: хук был длиннее "
                         f"{self.max_hook_words} слов или в тексте появились факты, "
                         "которых нет в посте. Сделай короче и строго по посту.")
        lines.append("Напиши сценарий клипа. Только JSON.")
        return "\n".join(lines)


def _load_system() -> str:
    """Стайлгайд канала + правила клипа. Спека: «промпт переиспользует
    style.md» — правила фактов у поста и клипа расходиться не должны."""
    style = Path(PROMPTS_DIR / "style.md").read_text("utf-8").strip()
    return f"{style}\n\n{CLIP_RULES.strip()}"


def _clean_hook(value: str) -> str:
    hook = " ".join(value.split()).strip(" \"«»'").rstrip(".!")
    if hook.isupper():                       # капслок целыми словами запрещён стайлгайдом
        hook = hook.capitalize()
    return hook


def _clean_line(value: str) -> str:
    """Строка субтитра. Длинную режем ПО СЛОВУ, а не по счётчику знаков.

    Замечено на приёмке: жёсткая обрезка оставляла «…по здоров» и «…при под» —
    и на плашке уродство, и озвучка читает обрубок вслух."""
    text = " ".join(str(value).split()).strip()
    if len(text) <= MAX_LINE_CHARS:
        return text
    cut = text[:MAX_LINE_CHARS]
    space = cut.rfind(" ")
    if space > MAX_LINE_CHARS // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:—-") + "…"


def _strip_handles(text: str) -> str:
    """Адрес канала в CTA ставит конвейер, а не модель: @ник из генерации —
    это ссылка неизвестно куда."""
    words = [w for w in text.split() if not w.startswith("@") and "t.me" not in w]
    cleaned = " ".join(words).strip(" —-·,")
    return cleaned or DEFAULT_CTA


def _get(row, key: str):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None
