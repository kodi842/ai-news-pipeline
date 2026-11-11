"""Rewriter (спека §5): кластер → пост канала.

Вход — весь кластер целиком, выход — заголовок-хук, текст в стиле канала,
категория и выбранное каскадом медиа. Стайлгайд и few-shot лежат отдельными
файлами и уходят в системный промпт ОДНИМ И ТЕМ ЖЕ текстом для всех городов —
иначе prompt caching (требование спеки) не переиспользуется между городами.

Главный запрет спеки — «не выдумывать факты» — держится тремя вещами:
  * промптом (правила и примеры, где показано, чего писать нельзя);
  * пометкой `low_facts`, когда фактуры в кластере нет;
  * автоматической сверкой: числа и имена собственные из драфта ищутся в
    материале, ненайденное попадает в `unsupported` и в флаг драфта.
Сверка — сеть безопасности, а не гарантия: последнее слово за модератором.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from tgtraff.llm.client import LlmError, NoFacts
from tgtraff.rewriter.media import MediaChoice, MediaSettings, choose_media
from tgtraff.rewriter.settings import CATEGORIES, URGENCIES, RewriterSettings

log = logging.getLogger("tgtraff.rewriter")

PROMPTS_DIR = Path(__file__).with_name("prompts")

# --- сверка фактов ----------------------------------------------------------

# Числа драфта должны быть в материале. Сравниваем по цифрам, потому что
# «№3» и «больницу №3» — одно и то же число в разном оформлении.
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")

# Записи времени и дат у источников разнобойны: «15.15» и «15:15», «03.09.2026»
# и «3 сентября» — один и тот же факт в разном оформлении. Приводим их к общему
# виду ДО сверки, иначе сверка ловит формат записи, а не выдумку (решение
# ревью §33; живой ложный флаг — драфт #54, «Сбор в 15.15» у example_channel_7).
# Трогаем только время и даты: «1.5 млн» остаётся одним числом, иначе «5»
# из него стало бы разрешённым фактом.
_DATE_DOTS_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b")
_TIME_DOT_RE = re.compile(r"\b(\d{1,2})\.(\d{2})(?=\D|$)")


def normalize_time(text: str) -> str:
    """«03.09.2026» → «03 09 2026», «15.15» → «15:15»."""
    return _TIME_DOT_RE.sub(r"\1:\2", _DATE_DOTS_RE.sub(r"\1 \2 \3", text or ""))


def _canon_number(token: str) -> str:
    """Ведущие нули незначащи: «09:00» источника и «9 часов» драфта — одно время."""
    return token.lstrip("0") or "0"


# Имя собственное: слово с заглавной буквы не в начале предложения (тот же
# приём, что у entity_keys дедупа, — иначе в набор попадёт каждое первое слово).
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?:;»)])\s+|\n+")
_ENTITY_MIN_LEN = 4
_ENTITY_KEY_LEN = 5

# Слова, которые пишутся с заглавной по правилам, а не потому что это фактура.
_ENTITY_STOP = frozenset("""
Аварийн Городск Полице Пожарн Спасател Сотрудник Мужчин Женщин Ребен Ребён Води
Пострадав Очевид Жител Подроб Предварительн Информац Ситуац Улиц Проспект Район
Больниц Скор Движен Специалист Работ Причин Утром Вечером Днём Днем Ночью Также
Кроме Однако Сейчас Позже Ранее После Около Более Менее Всего Первый Второй
""".split())


def _numbers(text: str) -> list[str]:
    return _NUMBER_RE.findall(normalize_time(text))


def _entities(text: str) -> list[str]:
    out: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        words = re.findall(r"[\w-]+", sentence, flags=re.U)
        for word in words[1:]:
            core = word.strip("-")
            if len(core) < _ENTITY_MIN_LEN or not core[:1].isupper() or core.isdigit():
                continue
            if any(core.startswith(stop) for stop in _ENTITY_STOP):
                continue
            out.append(core)
    return out


# Хвостовая гласная снимается: «ДОЗе» в источнике и «ДОЗа» в драфте — одно и
# то же место, а не выдуманное. Без этого сверка ловит падежи, а не выдумки.
_TAIL_VOWELS = "аеиоуыэюяьй"


def _key(word: str) -> str:
    core = word.lower().replace("ё", "е")[:_ENTITY_KEY_LEN]
    return core.rstrip(_TAIL_VOWELS) or core


def unsupported_facts(draft_text: str, source_texts: list[str]) -> list[str]:
    """Числа и имена драфта, которых нет ни в одном тексте кластера."""
    material = "\n".join(source_texts)
    known_numbers = {_canon_number(n) for n in _numbers(material)}
    known_keys = {_key(w) for w in re.findall(r"[\w-]+", material, flags=re.U)}

    missing: list[str] = []
    for number in _numbers(draft_text):
        # «00» из записи времени фактом не является: источник пишет «в 22 часа»,
        # драфт — «в 22:00», и это одно и то же (живая регрессия драфта #61).
        if not number.strip("0"):
            continue
        if _canon_number(number) not in known_numbers:
            missing.append(number)
    for word in _entities(draft_text):
        if _key(word) not in known_keys:
            missing.append(word)
    seen: list[str] = []
    for item in missing:
        if item not in seen:
            seen.append(item)
    return seen


# --- драфт ------------------------------------------------------------------

@dataclass
class Draft:
    cluster_id: int
    city: str
    title: str
    body: str
    category: str
    urgency: str
    district: str | None
    media: MediaChoice
    flags: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    note: str = ""
    model: str = ""
    cost_usd: float = 0.0
    sources: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.body}"


class Rewriter:
    def __init__(self, client, settings: RewriterSettings | None = None,
                 media_settings: MediaSettings | None = None):
        self.client = client
        self.settings = settings or RewriterSettings()
        self.media_settings = media_settings or MediaSettings()
        self._system = _load_system(self.settings)

    @property
    def system_prompt(self) -> str:
        return self._system

    def rewrite(self, material, detector, db=None) -> Draft:
        data, reply = self.client.ask_json(
            system=self._system, user=self._prompt(material),
            max_tokens=self.settings.max_tokens, purpose="rewrite")

        title = str(data.get("title") or "").strip()
        body = str(data.get("body") or "").strip()
        if not title or not body:
            # Модель посмотрела материал и сказала: писать не из чего. Это
            # РЕШЕНИЕ по кластеру, а не сбой запроса — повтор через пять минут
            # даст ровно тот же ответ за те же деньги.
            raise NoFacts(str(data.get("note") or "").strip()
                          or "модель вернула пустой драфт")

        category = str(data.get("category") or "").strip().lower()
        if category not in CATEGORIES:
            log.debug("кластер #%s: неизвестная категория %r", material.cluster_id, category)
            category = "other"
        urgency = str(data.get("urgency") or "normal").strip().lower()
        if urgency not in URGENCIES:
            urgency = "normal"

        flags: list[str] = []
        if material.is_low_facts or bool(data.get("low_facts")):
            flags.append("low_facts")

        unsupported: list[str] = []
        if self.settings.check_facts:
            unsupported = unsupported_facts(f"{title}\n{body}", material.fact_texts)
            if unsupported:
                flags.append("unsupported_facts")

        media = choose_media(material, detector, self.media_settings, db=db)
        if media.needs_check:
            flags.append("media_unverified")

        return Draft(
            cluster_id=material.cluster_id, city=material.city, title=title, body=body,
            category=category, urgency=urgency,
            district=(data.get("district") or None), media=media, flags=flags,
            unsupported=unsupported, note=str(data.get("note") or "").strip(),
            model=getattr(reply, "model", ""), cost_usd=getattr(reply, "cost_usd", 0.0),
            sources=material.sources,
        )

    # ---------- промпт ----------

    def _prompt(self, material) -> str:
        posts = material.posts[:self.settings.max_posts]
        limit = self.settings.max_chars_per_post
        lines = [f"Город: {material.city_name}",
                 f"Источников: {len(material.sources)}, публикаций: {len(material.posts)}",
                 f"Медиа в кластере: {len(material.media)}", ""]
        for i, p in enumerate(posts, 1):
            tag = "официальный источник" if p.official else p.platform
            lines.append(f"[{i}] {p.source_key} ({tag}), {p.published_at[:16].replace('T', ' ')} UTC")
            if p.is_placeholder:
                extra = f", медиа: {len(p.media)}" if p.media else ""
                lines.append(f"    (публикация без текста{extra})")
            else:
                text = p.text[:limit]
                lines.append("    " + text.replace("\n", "\n    "))
            lines.append("")
        lines.append(f"Напиши пост канала «ЧП {material.city_name}» по этому материалу. "
                     "Только JSON.")
        return "\n".join(lines)


def _load_system(settings: RewriterSettings) -> str:
    style = Path(settings.style_path) if settings.style_path else PROMPTS_DIR / "style.md"
    few_shot = (Path(settings.few_shot_path) if settings.few_shot_path
                else PROMPTS_DIR / "few_shot.md")
    parts = [style.read_text("utf-8").strip()]
    if few_shot.exists():
        parts.append(few_shot.read_text("utf-8").strip())
    return "\n\n".join(parts)
