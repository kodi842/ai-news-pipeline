-- Схема хранилища конвейера (спека: docs/PROJECT-SPEC.md, «Архитектура»).
-- Таблицы clusters/drafts/published/subs_metrics/invite_links заводятся здесь по спеке,
-- но наполняются фазами 3-6; их колонки уточняются в своих фазах.

CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    city          TEXT NOT NULL,
    source_type   TEXT NOT NULL,            -- tg | tg_regional | vk | rss | official
    key           TEXT NOT NULL,            -- username канала / screen_name паблика / url
    query         TEXT,                     -- исходный поисковый запрос для status=resolve
    title         TEXT,
    note          TEXT,
    status        TEXT NOT NULL DEFAULT 'verified',   -- verified | resolve | dormant | dead
    priority      TEXT,
    city_filter   TEXT,                     -- областные источники: слово-фильтр по городу
    enabled       INTEGER NOT NULL DEFAULT 1,
    tg_peer_id    INTEGER,                  -- разрешённый peer (кэш)
    joined_at     TEXT,                     -- когда юзербот подписался (прогрев)
    last_post_at  TEXT,                     -- для алерта «источник молчит > 24 ч»
    last_polled_at TEXT,                    -- поллинг ВК/RSS/ТГ-фолбэка: когда опрашивали
    last_error    TEXT,                     -- последняя ошибка опроса (источник недоступен)
    http_etag     TEXT,                     -- условный GET для RSS: не тянуть ленту зря
    http_last_modified TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (city, source_type, key)
);

-- Сырые посты источников. UNIQUE(source_id, external_id) — техническая гарантия
-- «рестарт не дублирует»: повторная запись того же сообщения невозможна.
CREATE TABLE IF NOT EXISTS raw_posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    source_type   TEXT NOT NULL,
    external_id   TEXT NOT NULL,            -- TG: message_id; VK: post id; RSS: guid
    grouped_id    TEXT,                     -- альбом (несколько сообщений = один пост)
    text          TEXT,
    url           TEXT,
    published_at  TEXT NOT NULL,            -- ISO8601 UTC
    edited_at     TEXT,
    collected_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    views         INTEGER,
    fwd_from      TEXT,
    has_media     INTEGER NOT NULL DEFAULT 0,
    media_state   TEXT NOT NULL DEFAULT 'none',   -- none|pending|done|failed|skipped|partial
    via           TEXT NOT NULL DEFAULT 'live',   -- live (событие) | catchup (догон после простоя)
    filtered_out  INTEGER NOT NULL DEFAULT 0,     -- областной источник, город не упомянут
    filter_reason TEXT,
    raw_json      TEXT,
    UNIQUE (source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_posts_published ON raw_posts(published_at);
CREATE INDEX IF NOT EXISTS idx_raw_posts_collected ON raw_posts(collected_at);
CREATE INDEX IF NOT EXISTS idx_raw_posts_media_state ON raw_posts(media_state);
CREATE INDEX IF NOT EXISTS idx_raw_posts_group ON raw_posts(source_id, grouped_id);

-- Медиа сырых постов. Всё качается локально: форвард/копипаст запрещены (CLAUDE.md),
-- публикация — перезаливом своего файла.
CREATE TABLE IF NOT EXISTS raw_media (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_post_id   INTEGER NOT NULL REFERENCES raw_posts(id) ON DELETE CASCADE,
    ord           INTEGER NOT NULL DEFAULT 0,
    kind          TEXT NOT NULL,            -- photo|video|document|audio|other
    source_url    TEXT,                     -- прямая ссылка (ВК/RSS); у ТГ медиа тянется Telethon
    file_path     TEXT,
    file_size     INTEGER,
    mime_type     TEXT,
    sha256        TEXT,
    phash         TEXT,                     -- perceptual hash картинки (дедуп, фаза 3)
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|done|failed|skipped
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (raw_post_id, ord)
);
CREATE INDEX IF NOT EXISTS idx_raw_media_status ON raw_media(status);
CREATE INDEX IF NOT EXISTS idx_raw_media_sha ON raw_media(sha256);
CREATE INDEX IF NOT EXISTS idx_raw_media_phash ON raw_media(phash);

-- Служебные отметки конвейера: когда заведена сессия (от неё считаются сутки
-- до сброса чужих авторизаций и до первых вступлений по инвайтам) и т.п.
CREATE TABLE IF NOT EXISTS meta (
    key           TEXT PRIMARY KEY,
    value         TEXT,
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Состояние плавного прогрева юзербота (подписка на источники по 5-10 в день).
CREATE TABLE IF NOT EXISTS warmup_state (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER NOT NULL UNIQUE REFERENCES sources(id) ON DELETE CASCADE,
    target        TEXT NOT NULL,            -- username публичного канала или инвайт-ссылка
    kind          TEXT NOT NULL DEFAULT 'username',  -- username | invite
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|joined|already_member|requested|failed|skipped|dead
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    joined_at     TEXT,
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Дневной план прогрева: квота выбирается один раз в день и переживает рестарт.
CREATE TABLE IF NOT EXISTS warmup_days (
    day           TEXT PRIMARY KEY,         -- YYYY-MM-DD в таймзоне прогрева
    quota         INTEGER NOT NULL,
    done          INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ---- таблицы следующих фаз (спека, «Хранилище»); в фазе 1 не наполняются ----

CREATE TABLE IF NOT EXISTS clusters (          -- фаза 3
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    city          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',   -- open (окно живо) | closed
    category      TEXT,
    lead_raw_post_id INTEGER REFERENCES raw_posts(id),
    text_hash     TEXT,
    size          INTEGER NOT NULL DEFAULT 1,     -- логических постов в кластере
    first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_seen_at  TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_clusters_window ON clusters(city, status, last_seen_at);

-- N:M кластер↔пост. Одна строка = один ЛОГИЧЕСКИЙ пост (у альбома это его
-- головное сообщение — то, что несёт текст); остальные сообщения альбома
-- достаются по grouped_id и отдельными участниками не считаются.
CREATE TABLE IF NOT EXISTS cluster_members (   -- фаза 3
    cluster_id    INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    raw_post_id   INTEGER NOT NULL REFERENCES raw_posts(id) ON DELETE CASCADE,
    similarity    REAL,                          -- с чем склеили: cosine или 1.0 у точного хэша
    reason        TEXT,                          -- exact_text | phash | semantic | new
    matched_raw_post_id INTEGER,                 -- к какому посту кластера присоединили
    added_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (cluster_id, raw_post_id)
);
CREATE INDEX IF NOT EXISTS idx_cluster_members_post ON cluster_members(raw_post_id);

-- Подписи постов для дедупа (фаза 3): точный хэш нормализованного текста и
-- вектор ЛОКАЛЬНОЙ модели. Считаются один раз на пост — отсюда инкрементальность:
-- новый пост сравнивается с окном готовых векторов, ничего не пересчитывается.
CREATE TABLE IF NOT EXISTS post_signatures (
    raw_post_id   INTEGER PRIMARY KEY REFERENCES raw_posts(id) ON DELETE CASCADE,
    city          TEXT NOT NULL,
    source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    published_at  TEXT NOT NULL,
    text_hash     TEXT,                          -- NULL: текста мало для точного хэша
    text_chars    INTEGER NOT NULL DEFAULT 0,
    embedding     BLOB,                          -- float32, L2-нормированный
    embed_model   TEXT,
    entities      TEXT,                          -- имена собственные через пробел
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_post_signatures_window ON post_signatures(city, published_at);
CREATE INDEX IF NOT EXISTS idx_post_signatures_hash ON post_signatures(text_hash);

-- Головное сообщение поста: у одиночного поста это он сам, у альбома —
-- сообщение с наименьшим message_id (так же выбирает голову нормализатор).
-- Через него дедуп видит альбом одним постом: и в очереди на обработку,
-- и при поиске по картинкам (медиа второго-пятого сообщения альбома —
-- такая же фактура кластера, как и медиа первого).
CREATE VIEW IF NOT EXISTS post_heads AS
SELECT p.id AS raw_post_id,
       CASE WHEN p.grouped_id IS NULL THEN p.id
            ELSE (SELECT h.id FROM raw_posts h
                   WHERE h.source_id = p.source_id AND h.grouped_id = p.grouped_id
                   ORDER BY CAST(h.external_id AS INTEGER), h.id LIMIT 1)
       END AS head_id
FROM raw_posts p;
CREATE TABLE IF NOT EXISTS drafts (            -- фаза 4-5
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id    INTEGER REFERENCES clusters(id) ON DELETE SET NULL,
    city          TEXT NOT NULL,
    title         TEXT,
    body          TEXT,
    category      TEXT,
    urgency       TEXT,
    safety_verdict TEXT,                       -- safe | manual_only | reject
    flags         TEXT,                        -- напр. low_facts, ugc
    media_path    TEXT,
    status        TEXT NOT NULL DEFAULT 'new', -- new|queued|approved|rejected|published
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS published (         -- фаза 5
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id      INTEGER REFERENCES drafts(id) ON DELETE SET NULL,
    city          TEXT NOT NULL,
    platform      TEXT NOT NULL DEFAULT 'tg',  -- tg | vk | ...
    channel       TEXT NOT NULL,
    message_id    TEXT,
    url           TEXT,
    -- Длинный пост уходит двумя сообщениями (медиа + текст): чтобы правка и
    -- удаление из админ-бота убирали ОБА, вспомогательные id лежат здесь.
    extra_message_ids TEXT,
    published_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    edited_at     TEXT,
    deleted_at    TEXT,
    UNIQUE (platform, channel, message_id)
);
CREATE INDEX IF NOT EXISTS idx_published_draft ON published(draft_id);

-- Очередь публикаций (спека §8). Лежит в БД, а не в памяти процесса: рестарт
-- publisher'а не имеет права ни потерять одобренный пост, ни выпустить его
-- дважды (за это отвечает claim: queued → sending одним UPDATE).
CREATE TABLE IF NOT EXISTS publish_queue (     -- фаза 5
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id      INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    city          TEXT NOT NULL,
    urgency       TEXT NOT NULL DEFAULT 'normal',   -- flash идёт вне сетки
    status        TEXT NOT NULL DEFAULT 'queued',   -- queued|sending|published|failed|cancelled
    scheduled_at  TEXT NOT NULL,                    -- ISO8601 UTC, слот города
    published_at  TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    moderator_id  INTEGER,                          -- кто нажал «Опубликовать»
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_publish_queue_due ON publish_queue(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_publish_queue_city ON publish_queue(city, status, scheduled_at);
-- Один драфт — одна живая запись очереди: защита от двойной постановки.
CREATE UNIQUE INDEX IF NOT EXISTS idx_publish_queue_draft ON publish_queue(draft_id)
    WHERE status IN ('queued','sending');
-- Читатели, присылающие новости в UGC-бот (фаза 5). Город запоминается по
-- deep-link'у `?start=mgn|nvk|che`, счётчик заявок нужен ручным выплатам
-- за эксклюзив (PINS.md §1: 300 ₽ за фото/видео, до 1000 ₽ за крупное ЧП).
CREATE TABLE IF NOT EXISTS ugc_senders (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    city          TEXT,
    submissions   INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_seen_at  TEXT
);

CREATE TABLE IF NOT EXISTS subs_metrics (      -- фаза 6
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    city          TEXT NOT NULL,
    platform      TEXT NOT NULL DEFAULT 'tg',
    channel       TEXT NOT NULL,
    captured_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    subscribers   INTEGER,
    views_24h     INTEGER,
    er            REAL
);
CREATE TABLE IF NOT EXISTS invite_links (      -- фаза 6
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    city          TEXT NOT NULL,
    channel       TEXT NOT NULL,
    name          TEXT NOT NULL,               -- метка источника трафика
    url           TEXT,
    joins_total   INTEGER NOT NULL DEFAULT 0,
    last_checked_at TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Учёт обращений к платной модели (фаза 4). Спека даёт фазе бюджет $1-3 на
-- калибровку и $1.2-2/день в проде — значит, потраченное должно быть видно
-- в базе, а не только в логе процесса.
CREATE TABLE IF NOT EXISTS llm_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    purpose       TEXT NOT NULL,            -- relevance | rewrite | safety | watermark
    model         TEXT NOT NULL,
    city          TEXT,
    cluster_id    INTEGER,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read    INTEGER NOT NULL DEFAULT 0,
    cache_write   INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_day ON llm_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_drafts_cluster ON drafts(cluster_id);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(city, status);

-- ---------------------------------------------------------------- фаза 6 ----

-- Прирост по инвайт-ссылкам по дням: `invite_links.joins_total` отвечает на
-- вопрос «сколько всего привела площадка», а эта таблица — «сколько привела
-- вчера». Без неё CPF считается только за всю жизнь ссылки, и просевший посев
-- не видно за накопленным числом.
CREATE TABLE IF NOT EXISTS invite_link_days (
    link_id       INTEGER NOT NULL REFERENCES invite_links(id) ON DELETE CASCADE,
    day           TEXT NOT NULL,               -- YYYY-MM-DD, локальные сутки города
    joins         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (link_id, day)
);
CREATE INDEX IF NOT EXISTS idx_invite_link_days_day ON invite_link_days(day);

-- Одна ссылка на источник трафика в канале города: повторный `ensure` находит
-- уже созданную, а не заводит вторую (иначе прирост размажется по двойникам).
CREATE UNIQUE INDEX IF NOT EXISTS idx_invite_links_name
    ON invite_links(city, channel, name);
CREATE INDEX IF NOT EXISTS idx_subs_metrics_city ON subs_metrics(city, captured_at);

-- ------------------------------------------------------------- фаза V1 ------

-- Клипы завода коротких видео (VIDEO-SPEC, фаза V1). Один клип — один драфт:
-- клип это тот же материал в другой упаковке, и вторая упаковка той же новости
-- в рекомендациях площадки выглядит спамом.
--
-- Очередь рендера лежит здесь, а не в памяти процесса: ffmpeg на 2 vCPU идёт
-- минутами, и рестарт контейнера не имеет права ни потерять клип, ни собрать
-- его дважды (за это отвечает claim: planned → rendering одним UPDATE).
CREATE TABLE IF NOT EXISTS clips (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id      INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    city          TEXT NOT NULL,
    mode          TEXT NOT NULL,               -- video | photos
    score         INTEGER,                     -- виральный потенциал 1-10 (LLM)
    score_reason  TEXT,
    hook          TEXT,                        -- плашка первых 1.5 сек, ≤6 слов
    lines         TEXT,                        -- JSON: строки субтитров/озвучки
    cta           TEXT,                        -- концовка-аутро
    voice         TEXT,                        -- голос TTS (режим photos)
    status        TEXT NOT NULL DEFAULT 'planned',
        -- planned|rendering|ready|sent|published|rejected|failed
    flags         TEXT,                        -- JSON: media_unverified и т.п.
    reject_reason TEXT,
    file_path     TEXT,
    file_size     INTEGER,
    duration_sec  REAL,
    subtitles     TEXT,                        -- whisper | tts | none
    media_kind    TEXT,                        -- video | photo
    media_ids     TEXT,                        -- JSON: raw_media, из которых собран
    source_key    TEXT,                        -- источник исходного медиа
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    moderator_id  INTEGER,
    model         TEXT,
    cost_usd      REAL,
    card_chat_id  INTEGER,
    card_message_id INTEGER,
    card_sent_at  TEXT,
    render_started_at TEXT,
    rendered_at   TEXT,
    decided_at    TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_clips_status ON clips(status, score DESC, id);
CREATE INDEX IF NOT EXISTS idx_clips_city_day ON clips(city, created_at);
-- Один живой клип на драфт: отклонённый и не собравшийся замок не держат —
-- перегенерить хук и собрать заново разрешено.
CREATE UNIQUE INDEX IF NOT EXISTS idx_clips_draft ON clips(draft_id)
    WHERE status IN ('planned','rendering','ready','sent','published');
