"""Карточка драфта для админ-бота (спека §7).

Карточка — это интерфейс решения: модератор смотрит её секунды, поэтому всё,
что может остановить публикацию, вынесено наверх — «молния», вердикт safety,
подозрение на выдуманный факт, нехватка фактуры, чужой знак на медиа.

Модуль отвечает только за текст и клавиатуру: aiogram-обвязка в `bot.py`,
решения — в `actions.py`.
"""
from __future__ import annotations

import html

from tgtraff import drafts as drafts_store
from tgtraff.moderation.actions import LOW_FACTS, NOT_NEWS
from tgtraff.publisher.format import CATEGORY_EMOJI

TEXT_LIMIT = 4096
BODY_LIMIT = 2500

# callback_data: <действие>:<draft_id>
PUBLISH, EDIT, REJECT, DEFER, NOW = "pub", "edit", "rej", "defer", "now"
REASON_PREFIX = "why"
CANCEL = "cancel"
# callback_data: pull:<N> — выдать N карточек из пула (§55); не про драфт.
PULL = "pull"
# §61.1: подтвердить/отменить постановку присланного фото с чужим знаком.
PHOTO_OK, PHOTO_NO = "photo", "nophoto"
# callback_data: pins:<город|all> — выпустить закрепы (CHANNEL-SETUP §60.5).
PINS = "pins"

CATEGORY_LABEL = {
    "dtp": "ДТП", "fire": "ПОЖАР", "crime": "КРИМИНАЛ", "utility": "КОММУНАЛКА",
    "weather": "ПОГОДА", "city": "ГОРОД", "other": "ПРОИСШЕСТВИЕ",
}

FLAG_LABEL = {
    "low_facts": "⚠️ мало фактуры — деталей в источниках почти нет",
    "unsupported_facts": "❗️ проверить факты: в источниках их не нашлось",
    "media_unverified": "🎞 видео не проверено на чужой знак",
    "ugc": "📸 прислал читатель (UGC)",
    "service": "🤖 сервисный автопост",
}

MEDIA_LABEL = {
    "photo": "фото", "video": "видео", "template": "шаблонная обложка",
    "document": "файл", "none": "без медиа",
}

MEDIA_REASON = {
    "official": "медиа ведомства",
    "clean": "фото без чужих знаков",
    "watermarked": "все медиа кластера с чужими знаками → своя обложка",
    "unverified": "не проверено на чужой знак",
    "no_media": "медиа в кластере нет",
    "ugc": "прислано читателем",
    "moderator": "прислал модератор (§61)",
}


def card_text(row, city_name: str | None = None, links=None) -> str:
    """Тело карточки. HTML — та же разметка, что у публикации.

    `links` (§61.2) — источники со ссылками на исходные публикации
    (`actions.source_links`). Без них список источников остаётся простым
    перечислением имён: старые драфты и источники без url так и показываются."""
    flags = drafts_store.decode_list(row["flags"])
    sources = drafts_store.decode_list(row["sources"])
    head = []
    if (row["urgency"] or "") == "flash":
        head.append("⚡️ <b>МОЛНИЯ</b> — выйдет сразу после кнопки, вне сетки")
    if row["origin"] == "ugc":
        author = row["ugc_username"] or row["ugc_user_id"] or "аноним"
        head.append(f"📸 <b>UGC</b> от @{html.escape(str(author))} — за эксклюзив платим вручную")

    category = CATEGORY_LABEL.get(row["category"] or "other", (row["category"] or "?").upper())
    emoji = CATEGORY_EMOJI.get(row["category"] or "other", "❗")
    body = (row["body"] or "")[:BODY_LIMIT]

    lines = [
        *head,
        f"{emoji} <b>{html.escape(row['title'] or '')}</b>",
        "",
        html.escape(body),
        "",
        f"#{row['id']} · {html.escape(city_name or row['city'])} · {category}"
        + (f" · {html.escape(row['district'])}" if row["district"] else ""),
    ]

    media = MEDIA_LABEL.get(row["media_kind"] or "none", row["media_kind"] or "без медиа")
    reason = MEDIA_REASON.get(row["media_reason"] or "", row["media_reason"] or "")
    lines.append(f"🖼 {media}" + (f" — {reason}" if reason else ""))

    verdict = row["safety_verdict"] or "?"
    reasons = drafts_store.decode_list(row["safety_reasons"])
    mark = {"safe": "✅", "manual_only": "🟡", "reject": "⛔️"}.get(verdict, "•")
    lines.append(f"{mark} safety: {verdict}" + (f" ({', '.join(reasons)})" if reasons else ""))

    if links:
        lines.append(f"📰 источники ({len(links)}): " + ", ".join(_anchor(l) for l in links))
        if any(l.private for l in links):
            lines.append("🔒 приватный источник (*) откроется только участнику канала")
    elif sources:
        lines.append(f"📰 источники ({len(sources)}): " + html.escape(", ".join(sources[:8])))
    for flag in flags:
        if flag in FLAG_LABEL:
            lines.append(FLAG_LABEL[flag])
    if row["notes"]:
        lines.append("📝 " + html.escape(str(row["notes"])[:400]))

    return "\n".join(lines)[:TEXT_LIMIT]


def _anchor(link) -> str:
    """Имя источника — ссылкой на исходную публикацию (§61.2). Компактно:
    сырых URL в тексте нет, у приватной ссылки — звёздочка-пометка.

    Имя приходит из чужого конфига и чужой ленты, поэтому экранируется всегда:
    сломать вёрстку карточки оно не имеет права."""
    name = html.escape(link.name) + ("*" if link.private else "")
    if not link.url:
        return name
    return f'<a href="{html.escape(link.url, quote=True)}">{name}</a>'


def card_keyboard(row):
    """Кнопки спеки §7. «Сейчас» — ручной обход сетки для срочного."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    draft_id = row["id"]
    publish = "⚡️ Опубликовать сейчас" if (row["urgency"] or "") == "flash" \
        else "✅ Опубликовать"
    rows = [
        [InlineKeyboardButton(text=publish, callback_data=f"{PUBLISH}:{draft_id}"),
         InlineKeyboardButton(text="✏️ Править", callback_data=f"{EDIT}:{draft_id}")],
        [InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"{REJECT}:{draft_id}"),
         InlineKeyboardButton(text="⏸ Отложить", callback_data=f"{DEFER}:{draft_id}")],
    ]
    if (row["urgency"] or "") != "flash":
        rows.append([InlineKeyboardButton(text="⚡️ Вне очереди, сейчас",
                                          callback_data=f"{NOW}:{draft_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reject_keyboard(row):
    """Причина отклонения решает судьбу кластера (решение ревью §28)."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    draft_id = row["id"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Не новость — больше не предлагать",
                              callback_data=f"{REASON_PREFIX}:{draft_id}:{NOT_NEWS}")],
        [InlineKeyboardButton(text="📉 Мало фактуры — вернуться, если появится",
                              callback_data=f"{REASON_PREFIX}:{draft_id}:{LOW_FACTS}")],
        [InlineKeyboardButton(text="← Назад", callback_data=f"{CANCEL}:{draft_id}")],
    ])


def photo_keyboard(row, owner: str | None = None):
    """Присланное фото с чужим знаком (§61.1): ставим или отменяем.

    Кнопки «убрать знак» здесь нет и не будет — спека §5 запрещает трогать
    чужие знаки, детектор умеет только сказать, что картинка чужая."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    draft_id = row["id"]
    whose = f" ({owner})" if owner else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📌 Всё равно поставить{whose}"[:64],
                              callback_data=f"{PHOTO_OK}:{draft_id}")],
        [InlineKeyboardButton(text="← Отменить", callback_data=f"{PHOTO_NO}:{draft_id}")],
    ])


def pins_keyboard(target: str = "all"):
    """Закреп уходит всем подписчикам боевого канала — до этого шага
    оператор видит план и подтверждает его кнопкой."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Выпустить закрепы",
                              callback_data=f"{PINS}:{target}")]])


def summary_text(summary: dict, city_names: dict[str, str] | None = None) -> str:
    """Сводка очереди для /queue без аргумента (§55): сколько ждёт решения по
    городам и категориям, сколько из этого уже висит карточками в чате.

    Последней строкой — автопосты §59: они прошли мимо карточек, и без этой
    строки модератор узнал бы о них только вечером из дайджеста."""
    auto = _auto_line(summary)
    if not summary["total"]:
        return "\n".join(["Очередь пуста — решений никто не ждёт.", *auto])
    names = city_names or {}
    lines = [f"📥 <b>Ждут решения: {summary['total']}</b> — в пуле {summary['pool']} "
             f"(из них отложено {summary['deferred']}), карточками в чате "
             f"{summary['on_cards']}"]
    for city, block in sorted(summary["cities"].items(), key=lambda kv: (-kv[1]["total"], kv[0])):
        cats = ", ".join(f"{CATEGORY_LABEL.get(c, c)} {n}" for c, n in sorted(
            block["categories"].items(), key=lambda kv: (-kv[1], kv[0])))
        lines.append(f"<b>{html.escape(names.get(city, city))}</b> {block['total']}: {cats}")
    lines.append("Свежие вперёд: /queue N — выдать N карточек из пула.")
    lines += auto
    return "\n".join(lines)


def _auto_line(summary: dict) -> list[str]:
    """Строка автопостов (§59). Показывается, только если режим включён:
    при пустом autopublish_categories «автопостов 0» — не новость, а шум."""
    if not summary.get("autopublish"):
        return []
    return [f"🤖 автопостом за сутки: {summary.get('auto_24h', 0)} "
            "(нейтральные категории, safety=safe, без флагов — §59)"]


def summary_keyboard(n: int):
    """Одна кнопка под сводкой — пачка по умолчанию, без набора команды."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📥 Выдать {n} свежих", callback_data=f"{PULL}:{n}")]])


def published_keyboard(row, url: str | None = None):
    """Карточка после публикации: пост можно поправить или удалить (спека §8)."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    draft_id = row["id"]
    rows = [[InlineKeyboardButton(text="✏️ Править пост", callback_data=f"{EDIT}:{draft_id}"),
             InlineKeyboardButton(text="🗑 Удалить пост", callback_data=f"del:{draft_id}")]]
    if url:
        rows.insert(0, [InlineKeyboardButton(text="🔗 Открыть пост", url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
