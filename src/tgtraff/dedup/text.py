"""Нормализация текста поста — вход обоих уровней дедупа (спека §3).

Один и тот же текст у разных источников почти никогда не совпадает побайтно:
у каждого свой футер («Подписаться»), своя подпись канала, свои хэштеги, ссылки
и эмодзи. Уровень 1 (точный хэш) заработает только если всё это снять.

Две функции наружу:
  * `text_fingerprint` — хэш для точного совпадения (агрессивная нормализация);
  * `embed_text`       — текст для модели эмбеддингов (мягкая: регистр и
    пунктуация остаются, модели с ними работают лучше).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

# Короткие тексты точным хэшем не склеиваем: «Доброе утро» у пяти источников —
# это не одна новость. Такие посты остаются уровню 2 и медиа-хэшу.
MIN_HASH_CHARS = 30

# Сколько символов текста отдаём модели: тема новости содержится в первых
# абзацах, а длинный хвост только размывает вектор (и замедляет кодирование).
EMBED_LIMIT = 600

_URL_RE = re.compile(r"(https?://\S+|www\.\S+|t\.me/\S+|vk\.com/\S+)", re.I)
_MENTION_RE = re.compile(r"@[A-Za-z0-9_]{3,}")
_HASHTAG_RE = re.compile(r"#[^\s#]+")
# Разметка ссылок ВК: [id35462855|Лариса Смородина], [club123|Паблик].
_VK_LINK_RE = re.compile(r"\[(?:id|club|public)\d+\|([^\]]*)\]")
# Хвост «Читать далее: …» пишут в одну строку с фактурой, поэтому режется отдельно.
_TAIL_RE = re.compile(
    r"\s*(читать\s+(далее|подробнее|полностью)|подробнее\s+(по\s+ссылке|на\s+сайте|здесь|тут)|"
    r"подробности\s+(по\s+ссылке|на\s+сайте))\b.*$",
    re.I | re.U,
)
# Строки-атрибуции: «Фото: МЧС», «Источник: 74.ru», «Видео: читатель».
_CREDIT_RE = re.compile(r"^\s*(фото|видео|фото/видео|источник|автор|текст)\s*[:：]", re.I | re.U)

# Служебные слова футеров. Ищем по основам: подписаться/подписывайтесь/подписка.
_CTA_STEMS = (
    "подпис", "прислать", "присылайте", "предложить новость", "сообщить новость",
    "наш канал", "наш телеграм", "наши новости", "больше новостей", "все новости",
    "написать нам", "связь с редакцией", "реклама и сотрудничество", "по вопросам рекламы",
    "бот для связи", "ваши новости", "новости города в", "читать в", "смотреть в",
)
# Короткая строка с призывом — футер; длинная фраза с тем же словом — фактура
# («жители подписались под обращением»), её трогать нельзя.
_CTA_MAX_WORDS = 6

# Строка, НАЧИНАЮЩАЯСЯ с призыва, служебная при любой длине: «Публикуйте на
# своих страницах во ВКонтакте фотографии города…» — это футер паблика, а не
# новость. Формы перечислены целиком, не основами: «Подписан контракт на
# строительство школы» начинается с той же основы, но это фактура.
_CTA_LEAD = frozenset("""
подпишись подпишитесь подписывайтесь подписаться подписывайся публикуйте публикуй
присылайте пришлите прислать присылай пишите напишите пиши делитесь поделитесь
ставьте сохраняйте сохрани заходите вступайте вступай читайте читай следите следи
подключайтесь задавайте сообщайте сообщите предлагайте жмите переходите переходи
""".split())

# Меню площадок в подвале поста: «ДТП Новокузнецка👇 Telegram | VK | MAХ».
# Строка из названий соцсетей через разделитель — навигация, а не фактура.
_PLATFORMS = frozenset("""
telegram телеграм тг vk вк вконтакте max мах ok ок одноклассники дзен zen
rutube youtube tiktok сайт канал группа паблик чат бот
""".split())

# Имена собственные для проверки «одна ли это новость»: сравниваем по началу
# слова, чтобы «Череповца» и «Череповце» считались одним словом.
_ENTITY_KEY_LEN = 6
_ENTITY_MIN_LEN = 4
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?:;»)])\s+|\n+")

_EMOJI_EXTRA = "‼⁉™℗℠…•·—–«»"


def _strip_emoji(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if (0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF
                or 0x2190 <= code <= 0x21FF or 0x2B00 <= code <= 0x2BFF
                or 0xFE00 <= code <= 0xFE0F or code in (0x200D, 0x20E3, 0xFFFD)
                or ch in _EMOJI_EXTRA):
            out.append(" ")
            continue
        if unicodedata.category(ch) in ("So", "Sk", "Cf", "Co"):
            out.append(" ")
            continue
        out.append(ch)
    return "".join(out)


# Кириллические двойники латиницы: «MAХ» в подвалах пишут смешанной раскладкой.
_HOMOGLYPHS = str.maketrans("абекмнорстух", "abekmhopctyx")


def _is_platform_menu(words: list[str]) -> bool:
    """Строка вида «Telegram | VK | MAX»: половина слов — названия площадок."""
    if len(words) < 2:
        return False
    hits = sum(1 for w in words
               if w.lower() in _PLATFORMS or w.lower().translate(_HOMOGLYPHS) in _PLATFORMS)
    return hits >= 2 and hits * 2 >= len(words)


def _is_service_line(line: str) -> bool:
    """Строка не несёт фактуры: атрибуция, призыв подписаться, голая ссылка."""
    if _CREDIT_RE.match(line):
        return True
    body = _HASHTAG_RE.sub(" ", _MENTION_RE.sub(" ", _URL_RE.sub(" ", _VK_LINK_RE.sub(" ", line))))
    body = _strip_emoji(body)
    words = [w for w in re.split(r"[^\w]+", body, flags=re.U) if w]
    if not words:                                   # только ссылки/теги/эмодзи/разметка
        return True
    low = body.lower()
    if words[0].lower() in _CTA_LEAD:
        return True
    if _is_platform_menu(words):
        return True
    if len(words) <= _CTA_MAX_WORDS and any(stem in low for stem in _CTA_STEMS):
        return True
    return False


# Подпись канала над меню площадок («ДТП Новокузнецка👇» + «Telegram | VK | MAX»)
# короткая: длинную строку с фактурой из-за соседства с меню терять нельзя.
_SIGNATURE_MAX_WORDS = 3


def strip_boilerplate(text: str | None) -> str:
    """Убирает всё, что добавил источник от себя: футеры, подписи, ссылки, эмодзи."""
    if not text:
        return ""
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = _TAIL_RE.sub("", raw_line)
        if _is_service_line(line):
            # Меню площадок — последняя строка подвала, а над ним стоит название
            # канала: «ДТП Новокузнецка👇 / Telegram | VK | MAX» — одна подпись.
            menu_words = [w for w in re.split(r"[^\w]+", _strip_emoji(line), flags=re.U) if w]
            if lines and _is_platform_menu(menu_words):
                previous = [w for w in re.split(r"[^\w]+", lines[-1], flags=re.U) if w]
                if len(previous) <= _SIGNATURE_MAX_WORDS:
                    lines.pop()
            continue
        line = _HASHTAG_RE.sub(" ", _MENTION_RE.sub(" ", _URL_RE.sub(" ", _VK_LINK_RE.sub(" ", line))))
        line = _strip_emoji(line)
        line = re.sub(r"[ \t ]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def normalize_text(text: str | None) -> str:
    """Агрессивная нормализация под точный хэш: только буквы, цифры и пробелы."""
    body = strip_boilerplate(text).lower().replace("ё", "е")
    body = re.sub(r"[^\w\s]", " ", body, flags=re.U)
    return re.sub(r"\s+", " ", body, flags=re.U).strip()


def text_fingerprint(text: str | None) -> str | None:
    """Хэш точного совпадения. None — текста слишком мало, чтобы на него опираться."""
    norm = normalize_text(text)
    if len(norm) < MIN_HASH_CHARS:
        return None
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def embed_text(text: str | None, limit: int = EMBED_LIMIT) -> str:
    """Текст для модели: без служебного мусора, но с регистром и пунктуацией."""
    body = strip_boilerplate(text)
    body = re.sub(r"\s*\n\s*", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:limit].strip()


def entity_keys(text: str | None) -> set[str]:
    """Имена собственные поста: города, фамилии, названия, улицы.

    Зачем: сводки полиции и пресс-релизы пишутся по одному шаблону, и модель
    честно даёт им 0.87 — при том что речь о разных людях в разных городах.
    Отличает такие новости фактура, то есть имена. Берём слова с заглавной
    буквы НЕ в начале предложения (иначе в набор попадут «Приметы» и
    «Полицейские» — общая часть шаблона) и сравниваем по первым буквам,
    чтобы падежи не мешали.
    """
    keys: set[str] = set()
    for sentence in _SENTENCE_SPLIT.split(strip_boilerplate(text)):
        words = re.findall(r"[\w-]+", sentence, flags=re.U)
        for word in words[1:]:                       # первое слово — начало предложения
            core = word.strip("-")
            if len(core) < _ENTITY_MIN_LEN or not core[:1].isupper() or core.isdigit():
                continue
            if core.isupper() and len(core) <= 3:    # аббревиатуры вроде ДТП пропускаем
                continue
            keys.add(core.lower().replace("ё", "е")[:_ENTITY_KEY_LEN])
    return keys
