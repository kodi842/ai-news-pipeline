#!/bin/sh
# Цикл разбора кластеров в драфты для контейнера `drafts`.
#
# Почему отдельным файлом, а не командой в docker-compose.yml: свёрнутый блок
# YAML (`>`) сохраняет переносы строк у более отступленных строк, имногострочная
# команда молча распалась на несколько — `--since` и `--daily-budget` до
# python не доходили вовсе. Оба предохранителя тихо отсутствовали. В файле
# такой ловушки нет, и его видно глазами.
#
# Настройки — через окружение (см. deploy/docker/README.md):
#   DRAFTS_SINCE_HOURS   окно свежести кластеров, ч (по умолчанию 48)
#   LLM_DAILY_BUDGET     суточный потолок расхода на модель, $ (4)
#   DRAFTS_INTERVAL_SEC  пауза между проходами, с (300)
set -u

SINCE_HOURS="${DRAFTS_SINCE_HOURS:-48}"
BUDGET="${LLM_DAILY_BUDGET:-4}"
INTERVAL="${DRAFTS_INTERVAL_SEC:-300}"

echo "drafts: окно ${SINCE_HOURS} ч, суточный потолок \$${BUDGET}, проход раз в ${INTERVAL} с"

while true; do
    SINCE=$(date -u -d "${SINCE_HOURS} hours ago" +%Y-%m-%dT%H:%M:%SZ)
    # `|| true` — неудачный проход (нет связи с моделью, выбран бюджет) не
    # должен ронять контейнер в рестарт-луп: следующий проход через паузу.
    python scripts/drafts.py --run --limit 40 --since "$SINCE" --daily-budget "$BUDGET" || true
    sleep "$INTERVAL"
done
