"""Дедупликация ленты (спека §3): два уровня поверх normalized_feed().

  * уровень 1 — точные хэши: нормализованный текст и perceptual hash картинок;
  * уровень 2 — семантика: эмбеддинги ЛОКАЛЬНОЙ модели sentence-transformers,
    cosine выше порога из конфига (старт 0.85) в окне 48 часов.

Результат — кластер «одна новость, N источников» в `clusters`/`cluster_members`.
Кластеризация кросс-платформенная (ТГ + ВК + RSS в одном кластере) и
инкрементальная: новый пост сравнивается только с окном, ничего не пересчитывается.
"""
from tgtraff.dedup.cluster import Decision, Deduplicator  # noqa: F401,E402
from tgtraff.dedup.settings import DedupSettings  # noqa: F401,E402
