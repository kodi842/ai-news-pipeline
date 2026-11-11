"""Карточка клипа для админ-бота (VIDEO-SPEC, фаза V1).

«Карточка клипа в админ-бот — видео-файл + сценарий + источник + площадки.
Кнопки [Публиковать] [Отклонить] [Перегенерить хук].»

В V1 публикация ручная и на неё отведено две минуты на клип. Значит карточка
обязана дать ровно то, что нужно за эти две минуты: файл, готовое описание для
площадки (копировать, а не придумывать), откуда взялись кадры и чего в клипе
проверить глазами.

Автопубликации клипов не существует: площадки строже своего канала, и решение
ревью на этот счёт отдельное.
"""
from __future__ import annotations

import html

from tgtraff.clips import store
from tgtraff.clips.settings import ClipSettings
from tgtraff.publisher.format import CATEGORY_EMOJI

CAPTION_LIMIT = 1024

# callback_data клипов. Префиксы свои: карточки клипов и карточки драфтов
# живут в одном чате, и «pub:12» не должен значить сразу два разных решения.
CLIP_PUBLISH, CLIP_REJECT, CLIP_REHOOK = "cpub", "crej", "chook"

MODE_LABEL = {"video": "живое видео", "photos": "слайдшоу из фото"}
SUBTITLE_LABEL = {
    "whisper": "субтитры с речи (Whisper)",
    "tts": "субтитры = озвучка (TTS)",
    "lines": "субтитры строками, БЕЗ ОЗВУЧКИ (edge-tts не ответил)",
    "none": "субтитров нет (в дорожке нет речи)",
}
FLAG_LABEL = {
    "media_unverified": "🎞 чужой знак на кадрах НЕ ПРОВЕРЕН — посмотрите сами",
    "unsupported_facts": "❗️ в сценарии факт, которого нет в посте — сверьте",
    "hook_long": "✂️ хук длиннее шести слов — можно перегенерить",
    "no_username": "⚠️ в аутро НЕТ @ника канала (каналы в тестовом режиме) — "
                   "публиковать такой клип бессмысленно",
}


def card_text(clip, city_name: str | None = None, draft=None,
              settings: ClipSettings | None = None) -> str:
    settings = settings or ClipSettings()
    lines_out = store.decode_lines(clip)
    flags = store.decode_flags(clip)
    category = (draft["category"] if draft is not None else None) or ""
    emoji = CATEGORY_EMOJI.get(category, "🎬")

    head = [f"{emoji} <b>КЛИП: {html.escape(clip['hook'] or '')}</b>", ""]
    head += [f"· {html.escape(line)}" for line in lines_out]
    head.append("")

    size_mb = (clip["file_size"] or 0) / 1048576
    head.append(
        f"#{clip['id']} · {html.escape(city_name or clip['city'])} · "
        f"{MODE_LABEL.get(clip['mode'], clip['mode'] or '?')} · "
        f"{clip['duration_sec'] or 0:.0f} с · {size_mb:.1f} МБ")
    head.append(f"⭐️ виральность {clip['score'] or '?'}/10"
                + (f" — {html.escape(clip['score_reason'])}" if clip["score_reason"] else ""))
    head.append("🔊 " + SUBTITLE_LABEL.get(clip["subtitles"] or "none", clip["subtitles"] or ""))
    if clip["source_key"]:
        head.append(f"🎥 кадры: {html.escape(clip['source_key'])}"
                    + (f" · драфт #{clip['draft_id']}" if clip["draft_id"] else ""))
    for flag in flags:
        if flag in FLAG_LABEL:
            head.append(FLAG_LABEL[flag])
    head.append("📤 выложить руками: " + ", ".join(settings.platforms))
    if clip["cost_usd"]:
        head.append(f"💵 ${clip['cost_usd']:.3f}")
    return "\n".join(head)[:CAPTION_LIMIT]


def description(clip, city_name: str | None = None, username: str | None = None) -> str:
    """Готовое описание под клип на площадке — копируется как есть.

    Ни хэштегов-простыней, ни призывов: короткая суть и адрес канала. Хэштег
    города оставлен один — по нему клип находят в поиске площадки."""
    lines = [clip["hook"] or "", "", *store.decode_lines(clip)]
    tail = clip["cta"] or ""
    if username and f"@{username}" not in tail:
        tail = f"{tail}\n@{username}".strip()
    city_tag = "#" + (city_name or clip["city"]).replace(" ", "").replace("-", "")
    return "\n".join([*lines, "", tail, city_tag]).strip()


def card_keyboard(clip):
    """Три кнопки спеки. Кнопки «опубликовать в ВК» здесь нет: в V1 заливка
    ручная (это осознанный шаг — сначала смотрим качество глазами)."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    clip_id = clip["id"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Публиковать (руками)",
                              callback_data=f"{CLIP_PUBLISH}:{clip_id}"),
         InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"{CLIP_REJECT}:{clip_id}")],
        [InlineKeyboardButton(text="🔁 Перегенерить хук",
                              callback_data=f"{CLIP_REHOOK}:{clip_id}")],
    ])
