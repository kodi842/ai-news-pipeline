"""Верификация записей `status: resolve` из sources.yaml.

Спека, «Контекст»: «Задача фазы 1-2: верифицировать записи со статусом resolve
(поиск ТГ юзерботом / вступление в приватные по инвайту с TGStat / VK API
groups.search) и проверить RSS-ленты».

Здесь — разбор и оценка (чистые функции, без сети). Живые запросы делает
scripts/verify_sources.py: ленты тянутся HTTP, каналы ищутся юзерботом,
паблики ВК — методами API.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

# Пути, по которым ленту держат чаще всего.
_FEED_PATHS = ("/rss", "/feed", "/rss.xml", "/feed.xml", "/rss/", "/feed/",
               "/index.rss", "/news/rss", "/export/rss")

_FEED_LINK = re.compile(
    r"""<link[^>]+ (?: rel=["']alternate["'][^>]*type=["']application/(?:rss|atom)\+xml["']
                     | type=["']application/(?:rss|atom)\+xml["'][^>]*rel=["']alternate["'] )
        [^>]*?href=["']([^"']+)["']""",
    re.I | re.X,
)
_HREF_IN_LINK = re.compile(r"""href=["']([^"']+)["']""", re.I)
_ALT_FEED_LINK = re.compile(r"""<link[^>]*type=["']application/(?:rss|atom)\+xml["'][^>]*>""", re.I)

_PRIVATE_INVITE = re.compile(r"^(?:https?://)?t\.me/(?:\+|joinchat/)", re.I)
_WORD = re.compile(r"[^\w]+", re.UNICODE)


# ---------- RSS ----------

def feed_candidates(url: str) -> list[str]:
    """Адреса, по которым имеет смысл поискать ленту у данного сайта/раздела.

    Порядок важен: сначала сам адрес (вдруг это уже лента), потом раздел,
    потом корень сайта — у ведомств лента часто висит под разделом пресс-центра."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    root = f"{parsed.scheme}://{parsed.netloc}"
    section = f"{root}{parsed.path.rstrip('/')}" if parsed.path.strip("/") else root

    out = [url.rstrip("/") if url.rstrip("/") != root else url]
    for base in (section, root):
        for path in _FEED_PATHS:
            out.append(f"{base}{path}")

    seen: set[str] = set()
    unique = []
    for candidate in out:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def autodiscovered_feeds(html: str, base_url: str) -> list[str]:
    """Ссылки на ленту, объявленные самой страницей (<link rel=alternate>).
    Надёжнее перебора путей: сайт сам говорит, где его лента."""
    found: list[str] = []
    for tag in _ALT_FEED_LINK.findall(html or ""):
        if "alternate" not in tag.lower():
            continue
        href = _HREF_IN_LINK.search(tag)
        if href:
            absolute = urljoin(base_url, href.group(1))
            if absolute not in found:
                found.append(absolute)
    return found


def looks_like_feed(body: bytes) -> bool:
    """Похоже ли тело ответа на ленту. Многие сайты на несуществующий /rss
    отдают 200 и обычную HTML-страницу — такое за ленту принимать нельзя."""
    head = (body or b"")[:2000].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return False
    return b"<rss" in head or b"<feed" in head or b"<rdf:rdf" in head


# ---------- Telegram ----------

def normalise_username(value: str | None) -> str | None:
    """@name / t.me/name / ссылка на пост → name. Инвайт в приватный канал
    юзернеймом не является: по нему нельзя ни читать, ни резолвить."""
    if not value:
        return None
    value = value.strip()
    if _PRIVATE_INVITE.match(value):
        return None
    value = re.sub(r"^https?://", "", value)
    value = re.sub(r"^t\.me/", "", value, flags=re.I)
    value = value.lstrip("@").split("/")[0].split("?")[0]
    return value or None


def _tokens(text: str) -> list[str]:
    return [t for t in _WORD.split((text or "").lower()) if t]


def score_channel(title: str, query: str) -> int:
    """Насколько найденный канал похож на то, что искали.

    Город в запросе обязателен: «Новокузнецк 360» вместо «Магнитогорск 360°» —
    не «почти то же самое», а другой город. Первое слово запроса у нас всегда
    город, поэтому без него счёт нулевой."""
    q_tokens = _tokens(query)
    t_tokens = _tokens(title)
    if not q_tokens or not t_tokens:
        return 0

    city = q_tokens[0]
    if city not in t_tokens:
        return 0

    overlap = len(set(q_tokens) & set(t_tokens))
    exact = 5 if _tokens(title) == q_tokens else 0
    prefix = 2 if " ".join(t_tokens).startswith(" ".join(q_tokens)) else 0
    return overlap * 2 + exact + prefix


# ---------- ВК: подбор screen_name ----------
#
# `groups.search` сервисному ключу недоступен (error 15: нужен пользовательский токен,
# а он запрещён решением ревью §16). Зато `utils.resolveScreenName` и `groups.getById`
# работают — значит паблик можно найти перебором правдоподобных адресов.

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}

# Городские сокращения, которыми паблики зовут себя чаще, чем полным названием.
_CITY_SHORT = {
    "magnitogorsk": ("mgn", "magnitogorsk", "mgn74"),
    "novokuznetsk": ("nvkz", "novokuznetsk", "nk"),
    "cherepovets": ("che", "cherepovets", "cher35"),
}

# Слова, которые в адресах пабликов почти всегда пишут по-английски.
_WORD_ALIASES = {
    "типичный": "typical", "подслушано": "overhear", "инцидент": "incident",
    "чп": "chp", "новости": "news", "чёрное": "black", "черное": "black",
    "белое": "white", "слух": "slukh", "происшествия": "chp",
}

_SCREEN_IN_NOTE = re.compile(r"(?:vk\.com/|^|\s)([a-z][a-z0-9_.]{2,30})(?=\s|$|\.|,|—|-)", re.I)


def translit(text: str) -> str:
    """Грубая транслитерация: нужна не красота, а правдоподобный адрес паблика."""
    out = []
    for ch in (text or "").lower():
        out.append(_TRANSLIT.get(ch, ch if ch.isalnum() or ch.isspace() else " "))
    return re.sub(r"\s+", " ", "".join(out)).strip()


def screen_name_in_note(note: str | None) -> str | None:
    """Адрес паблика, если он уже написан в заметке конфига (vk.com/xxx или просто xxx)."""
    if not note:
        return None
    for match in _SCREEN_IN_NOTE.finditer(note):
        candidate = match.group(1).strip(".")
        if "_" in candidate or "vk.com/" in note[: match.start() + 8]:
            return candidate.lower()
    return None


def vk_candidates(query: str, note: str | None = None, city: str | None = None) -> list[str]:
    """Правдоподобные screen_name для запроса вида «Типичный Новокузнецк».

    Порядок осмысленный: сначала адрес из заметки (его писал человек), затем
    транслитерация и её варианты с английскими словами и сокращением города."""
    out: list[str] = []

    from_note = screen_name_in_note(note)
    if from_note:
        out.append(from_note)

    words = [w for w in translit(query).split() if len(w) > 1]
    ru_words = [w for w in re.split(r"[^\w]+", (query or "").lower()) if w]
    aliased = [_WORD_ALIASES.get(w, translit(w)) for w in ru_words if len(translit(w)) > 1]

    shorts = _CITY_SHORT.get(city or "", ())
    city_translit = translit(city or "")

    def add(*parts: str) -> None:
        for sep in ("_", "", "."):
            name = sep.join(p for p in parts if p)
            if name and name not in out and len(name) <= 40:
                out.append(name)

    if words:
        add(*words)
    if aliased and aliased != words:
        add(*aliased)
    # Формы с сокращением города вместо полного названия.
    for short in shorts:
        head = [w for w in aliased if w not in (city_translit, *shorts)]
        if head:
            add(*head, short)
            add(short, *head)
    return out
