"""Клип-фильтр: второй контур, СТРОЖЕ канального (VIDEO-SPEC, правило 3).

«Площадки банят шок-контент жёстче ТГ. В клипы идут только safety=safe драфты,
и дополнительно режутся кровь/тела/паника.»

Отсюда три отличия от `safety/rules.py`:

  1. проходит только вердикт `safe`. В канал `manual_only` выходит по кнопке
     человека — в клип не выходит никак;
  2. работает свой стоп-словарь. В нём нет ни одного слова, которое канальный
     фильтр считал бы опасным: это ровно та прослойка, что для своего канала
     допустима, а для рекомендаций ВК/OK/Rutube — нет (кровь, тела, паника,
     давка, гибель детей, «зажало в салоне»);
  3. драфт с пометкой `low_facts` в клип не идёт: сценарию нужны 2-3 строки
     фактов, а этот флаг означает, что фактуры нет вовсе.

Чего фильтр НЕ умеет: смотреть на сами кадры. Жесть в видео он не увидит —
именно поэтому в V1 каждый клип перед публикацией смотрит человек
(VIDEO-SPEC: «Автопаба клипов НЕТ до отдельного решения ревью»).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Категории-фавориты спеки: с них начинается отбор кандидатов (см. `score.py`).
FAVORITE_CATEGORIES = ("dtp", "fire", "weather", "city")

# Флаги драфта, при которых клипа не будет. Только `low_facts`: сценарию нужны
# 2-3 строки фактов, а этот флаг означает «фактуры в кластере нет вовсе».
#
# `unsupported_facts` СПЕЦИАЛЬНО не здесь. Это предупреждение автосверки фазы 4,
# у неё есть ложные срабатывания (форматы дат, §33), драфт с ним уже вышел в
# канал по кнопке человека, а сценарий клипа сверяется с текстом драфта
# отдельно (script.py). Пометка едет в карточку клипа, но клип не запрещает —
# иначе на живой ленте выбывает каждый четвёртый кандидат.
BLOCKING_FLAGS = ("low_facts",)
# Пометки, которые едут в карточку клипа, но решения не меняют.
WARNING_FLAGS = ("unsupported_facts", "media_unverified")

# Стоп-словарь клипов. Каждая строка — (что это, регулярка). Правила пишутся по
# основам слов; текст и словарь приводятся к «е», как в канальном фильтре.
_CLIP_STOP: tuple[tuple[str, str], ...] = (
    ("кровь", r"(кров[ьи]\b|кровью|окровавл\w+|в крови\b|лужа кров\w+|кровавы\w+)"),
    # «Тело» — только в контексте погибшего: «тело новости» и «тело письма»
    # фактурой ЧП не являются, а вычёркивать по одному слову — терять клипы.
    ("тела погибших",
     r"(тел[оа]\s+(погибш|мужчин|женщин|человек|ребен|ребён|подростк|пенсионер|"
     r"водител|пассажир|жертв)\w*|тел[оа]\s+(нашли|обнаружил\w+|извлекл\w+|"
     r"достал\w+|увезл\w+)|тела\s+(погибших|жертв)|труп\w*|останк\w+|"
     r"накрыл\w+ простын\w+|накрыт\w+ простын\w+|мешк\w+ для тел)"),
    ("паника и давка",
     r"(паник\w+|в панике|давк\w+|толпа (бежал|ринул|снесл)\w+|люди кричал\w+|"
     r"крики о помощи)"),
    # Гибель ребёнка — тема, на которой площадки закрывают клип и аккаунт
    # быстрее всего; в канале такая новость выходит, в рекомендации не идёт.
    ("гибель ребёнка",
     r"(погиб\w*\s+(ребен\w+|ребён\w+|мальчик\w*|девочк\w*|подросток|младенец|"
     r"школьник\w*)|(ребен\w+|ребён\w+|мальчик\w*|девочк\w*|младенец)\s+погиб\w*|"
     r"смерть (ребен\w+|ребён\w+|мальчик\w+|девочк\w+))"),
    ("подробности гибели",
     r"(зажал\w+ в [^.!?]{0,30}?(салоне|машине|кабине|автомобиле)|"
     r"сгорел\w* (человек|заживо|в машине|в салоне)|"
     r"оторвал\w+ (руку|ногу|конечност\w+)|раздавил\w+ насмерть|"
     r"скончал\w+ на месте от|умер\w* на руках)"),
)

_COMPILED = tuple((label, re.compile(pattern.replace("ё", "е"), re.I | re.U))
                  for label, pattern in _CLIP_STOP)


@dataclass(frozen=True)
class ClipVerdict:
    """Годится ли драфт в клип. `reasons` — человеческие причины отказа:
    они уходят в лог и в отчёт, чтобы отказ можно было проверить глазами."""

    ok: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def stop_hits(text: str | None) -> list[str]:
    """Что из клип-словаря нашлось в тексте (метки, а не сырые совпадения)."""
    body = (text or "").lower().replace("ё", "е").replace(" ", " ")
    return [label for label, pattern in _COMPILED if pattern.search(body)]


def check(draft, blocked: tuple[str, ...] = (),
          require_published: bool = False) -> ClipVerdict:
    """Вердикт по драфту (строка БД или словарь с теми же ключами).

    `require_published` — спека берёт в клипы «драфты, опубликованные в ТГ»;
    отбор кандидатов делает это запросом, а флаг оставлен для прямой проверки.
    """
    reasons: list[str] = []

    verdict = _get(draft, "safety_verdict")
    if verdict != "safe":
        reasons.append(f"safety={verdict or 'нет вердикта'} — в клип идёт только safe")

    flags = _flags(_get(draft, "flags"))
    for flag in BLOCKING_FLAGS:
        if flag in flags:
            reasons.append(f"{flag} — сомнительная фактура в клип не идёт")

    text = f"{_get(draft, 'title') or ''}\n{_get(draft, 'body') or ''}"
    hits = stop_hits(text)
    if hits:
        reasons.append("жесть для площадок: " + ", ".join(hits))

    category = _get(draft, "category") or "other"
    if blocked and category in blocked:
        reasons.append(f"категория {category} выключена из клипов конфигом")

    if require_published and _get(draft, "status") != "published":
        reasons.append("драфт не опубликован в ТГ — клип делается по вышедшей новости")

    return ClipVerdict(ok=not reasons, reasons=reasons)


def _get(row, key: str):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _flags(value) -> set[str]:
    if not value:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(v) for v in value}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {str(value)}
    return {str(v) for v in parsed} if isinstance(parsed, list) else {str(parsed)}
