#!/usr/bin/env bash
# Railway entrypoint: запускает в одном контейнере и бота-агента, и дашборд.
# Оба процесса делят один SQLite-файл и каталог сессий, поэтому должны жить
# вместе. Бот — в фоне с авто-рестартом, дашборд — в foreground (держит
# контейнер живым и слушает $PORT).

set -u

# Бот-агент в фоне. При падении — перезапуск через 15с, чтобы разовая
# сетевая ошибка не убивала весь сбор лидов до следующего деплоя.
(
  while true; do
    echo "[railway_start] starting multi_agent.py"
    python multi_agent.py
    echo "[railway_start] multi_agent.py exited (code $?), restart in 15s"
    sleep 15
  done
) &

# Дашборд в foreground.
exec gunicorn dashboard:app --bind "0.0.0.0:${PORT:-8080}" --workers 2 --threads 4 --timeout 120
