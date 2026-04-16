# Task Broker (вариант B)

Отдельный сервис: REST API на `127.0.0.1` + Telegram-бот для сотрудников. Личный бот OpenClaw только получает исходящие `sendMessage` при закрытии задачи (без второго `getUpdates`).

## Быстрый старт на Linux (VDS)

1. См. [deploy/setup-server.sh](deploy/setup-server.sh) и создайте пользователя `taskbroker`.
2. Скопируйте проект в `/opt/task-broker`, заполните `.env` из [.env.example](.env.example).
3. `python3 -m venv venv && venv/bin/pip install -r requirements.txt`
4. Установите unit: `sudo cp deploy/task-broker.service /etc/systemd/system/` → `daemon-reload` → `enable --now task-broker`.
5. Убедитесь, что процесс OpenClaw (gateway) запущен от пользователя с доступом к `curl http://127.0.0.1:8089` или настройте группу.

## OpenClaw

Скопируйте skill из [openclaw-skill/taskflow-broker/](openclaw-skill/taskflow-broker/) в `~/.openclaw/workspace/.agents/skills/` (или в `skills/` workspace).

## Проверка

[scripts/e2e-curl.sh](scripts/e2e-curl.sh) — примеры `curl` после регистрации контакта в staff-боте.

## Переменные окружения

| Переменная | Назначение |
|------------|------------|
| `STAFF_BOT_TOKEN` | Бот для сотрудников (long polling только здесь) |
| `CHAIRMAN_BOT_TOKEN` | Личный бот OpenClaw — только API sendMessage |
| `CHAIRMAN_CHAT_ID` | Ваш numeric user id в Telegram |
| `BROKER_API_SECRET` | Bearer для REST |
| `BROKER_HOST` / `BROKER_PORT` | По умолчанию `127.0.0.1:8089` |
| `DATABASE_PATH` | Путь к SQLite |
| `REMINDER_TIMEZONE` | Часовой пояс окна напоминаний (по умолчанию `Europe/Moscow`) |
| `REMINDER_WINDOW_START_HOUR` / `REMINDER_WINDOW_END_HOUR` | Окно отправки `[start, end)` (например 8–19) |
| `LONG_TASK_REMINDER_MIN_HOURS` | Порог «длинной» задачи для периодических напоминаний до срока (по умолчанию 72) |
| `MAX_GROUP_MEMBERS` | Лимит участников группы при fan-out |
