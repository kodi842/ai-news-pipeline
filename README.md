# tgtraff — конвейер сетки региональных ТГ-каналов

Пилот: Магнитогорск, Новокузнецк, Череповец. Пилот на трёх городах.

**Готово: фазы 1-5** — сбор (TG/VK/RSS), дедуп, рерайт и safety на Haiku,
модерация в админ-боте, публикация через Bot API, UGC-приём.

## Установка

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
cp .env.example .env      # и заполнить TG_API_ID / TG_API_HASH с my.telegram.org
```

## Запуск

```bash
.venv/bin/python scripts/login.py              # первый вход юзербота (интерактивно)
.venv/bin/python scripts/warmup.py             # плавная подписка: 5-10 каналов в день
.venv/bin/python scripts/collector.py          # сбор постов (TG события + ВК + RSS)
.venv/bin/python scripts/dedup.py              # склейка дублей в кластеры
.venv/bin/python scripts/drafts.py --run       # кластеры → драфты (Haiku)
.venv/bin/python scripts/moderation.py         # админ-бот модерации + публикация
.venv/bin/python scripts/ugc.py                # бот «Прислать новость»
.venv/bin/python scripts/clips.py --once       # завод коротких видео (VIDEO-SPEC V1)
.venv/bin/python scripts/status.py             # что уже в базе
.venv/bin/python -m pytest                     # тесты
```

`warmup.py --status` — состояние прогрева, `warmup.py --once` — одна подписка (для cron).
`moderation.py --status` — куда публикуем и что в очередях, `--once` — один тик без polling.
`clips.py --status` — очередь клипов, `--candidates` — кто мог бы стать клипом и что мешает
(без единого платного запроса). Для клипов нужен ffmpeg в PATH (в образе он есть).

**Публикация идёт только в тестовые каналы**, пока `config/channels.yaml` в режиме
`mode: test`; боевые id из `.env` в этом режиме не разрешаются вовсе.

## Что где

| Файл | Зачем |
|---|---|
| `config/sources.yaml` | города и источники (ведёт стратегическая сессия) |
| `config/settings.yaml` | параметры процесса: активные города, пути, прогрев, коллектор |
| `src/tgtraff/config.py` | загрузка конфига, `.env` |
| `src/tgtraff/schema.sql` | схема SQLite (таблицы из спеки + `raw_media`, `warmup_*`) |
| `src/tgtraff/db.py` | доступ к БД; здесь же гарантии «не теряем/не дублируем» |
| `src/tgtraff/collector/readonly.py` | механический запрет записи для юзербота |
| `src/tgtraff/collector/ingest.py` | сообщение → запись `raw_posts` (чистые функции) |
| `src/tgtraff/collector/tg_collector.py` | событийный сбор, догон после простоя, очередь медиа |
| `src/tgtraff/warmup.py` | план прогрева и цикл подписок |
| `config/channels.yaml` | куда публикуем: город → канал, слоты, лимиты, режим test/live |
| `src/tgtraff/dedup/` | склейка дублей: точные хэши, phash картинок, эмбеддинги |
| `src/tgtraff/rewriter/` | рерайт кластера в пост канала, медиа-каскад, обложки |
| `src/tgtraff/safety/` | стоп-словарь и классификатор публикуемости |
| `src/tgtraff/moderation/` | админ-бот, карточки, решения модератора, UGC-приём |
| `src/tgtraff/publisher/` | очередь со слотами, формат-шаблоны, доставки ТГ и ВК |

## Правила, которые держит код

- Юзербот **только читает**: `ReadOnlyTelegramClient` бросает `ReadOnlyViolation` на любой
  пишущий RPC (send/forward/edit/delete/invite/upload). Публикация — только Bot API (фаза 5).
- Медиа **скачивается локально** (перезалив своим файлом; форвард запрещён).
- Секреты — в `.env` и `secrets/`, в git не попадают.
- **Автопубликации нет**: в канал уходит только то, по чему нажата кнопка модератора
  (`safety.autopublish_categories: []`). Исключение — сервисные автопосты (утренняя
  погода и вечерний топ-5): это наш собственный контент без фактуры источников.
- Чужие вотермарки не трогаются: медиа с чужим знаком уходит вниз каскада, вместо него
  выходит своя обложка. Удаления/кропа знаков в коде нет (проверяется тестом).
