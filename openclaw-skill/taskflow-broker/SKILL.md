---
name: taskflow-broker
description: Поручения сотрудникам через локальный Task Broker REST (вариант B); не использовать getUpdates и не дублировать staff-бот.
---

# Taskflow Broker (OpenClaw)

## Константы (на сервере VDS)

- Base URL: `http://127.0.0.1:8089` (или порт из `BROKER_PORT` в `.env` брокера).
- Секрет: переменная окружения **`TASKFLOW_BROKER_SECRET`** на хосте (тот же текст, что `BROKER_API_SECRET` у брокера). **Не проси пользователя прислать секрет в чат.**

Пример заголовка: `Authorization: Bearer $TASKFLOW_BROKER_SECRET`

## Блок A — роль

Ты помогаешь руководителю ставить поручения сотрудникам. Источник правды по задачам — локальный Task Broker. Всегда вызывай API с заголовком `Authorization: Bearer` из окружения хоста. Не используй `getUpdates` и не создавай второй процесс для Telegram: доставка сотрудникам делает только брокер.

## Блок B — группы контактов

1. Список: `GET /groups`
2. Создать группу (участники — только зарегистрированные в `contacts`, `tg_user_id` как строка):

```bash
curl -sS -H "Authorization: Bearer $TASKFLOW_BROKER_SECRET" -H "Content-Type: application/json" \
  -d '{"name":"Бригада А","member_tg_ids":["111","222"]}' \
  http://127.0.0.1:8089/groups
```

3. Добавить/убрать участников: `PATCH /groups/{group_id}` с телом `{"add_member_tg_ids":["333"],"remove_member_tg_ids":[]}`

## Блок C — создание поручения

Ровно **одно** из полей: `assignee_tg_id`, `assignee_name` (подстрока ФИО), `assignee_group_id` (UUID группы).

1. `curl -sS -H "Authorization: Bearer $TASKFLOW_BROKER_SECRET" http://127.0.0.1:8089/contacts` — сопоставление исполнителя или проверка `member_tg_ids` для группы.
2. Одному исполнителю (JSON):

```bash
curl -sS -H "Authorization: Bearer $TASKFLOW_BROKER_SECRET" -H "Content-Type: application/json" \
  -d '{"assignee_name":"Иванов И.И.","title":"...","body":"...","due_at":"2026-04-10T15:00:00Z"}' \
  http://127.0.0.1:8089/tasks/json
```

3. Группе (fan-out: у каждого участника своя задача):

```bash
curl -sS -H "Authorization: Bearer $TASKFLOW_BROKER_SECRET" -H "Content-Type: application/json" \
  -d '{"assignee_group_id":"<GROUP_UUID>","title":"...","body":"...","due_at":"2026-04-20T12:00:00Z"}' \
  http://127.0.0.1:8089/tasks/json
```

Ответ: `batch_id`, массив `tasks` с `task_id` и `assignee_tg_id` по каждому участнику. При HTTP 409 `ambiguous_name` — перечисли варианты из `matches`. При ошибке — кратко статус и тело ответа.

Multipart `POST /tasks` поддерживает те же поля формы, включая `assignee_group_id`; вложения дублируются в каждую созданную задачу.

## Блок D — статус, отчёты, напоминания председателя

```bash
curl -sS -H "Authorization: Bearer $TASKFLOW_BROKER_SECRET" "http://127.0.0.1:8089/tasks"
curl -sS -H "Authorization: Bearer $TASKFLOW_BROKER_SECRET" "http://127.0.0.1:8089/tasks?status=sent"
curl -sS -H "Authorization: Bearer $TASKFLOW_BROKER_SECRET" "http://127.0.0.1:8089/tasks/<task_id>"
```

Сводный файл (CSV с BOM или Markdown, query `format=csv|md`):

```bash
curl -sS -H "Authorization: Bearer $TASKFLOW_BROKER_SECRET" -o summary.csv \
  "http://127.0.0.1:8089/reports/tasks-summary?scope=all&format=csv"
curl -sS -H "Authorization: Bearer $TASKFLOW_BROKER_SECRET" -o user.csv \
  "http://127.0.0.1:8089/reports/tasks-summary?scope=user&assignee_tg_id=123&format=csv"
curl -sS -H "Authorization: Bearer $TASKFLOW_BROKER_SECRET" -o group.csv \
  "http://127.0.0.1:8089/reports/tasks-summary?scope=group&group_id=<GROUP_UUID>&format=md"
```

Напоминание исполнителям от имени председателя (только в локальном окне 8:00–19:00 по `REMINDER_TIMEZONE`, иначе HTTP 400 с пояснением):

```bash
curl -sS -X POST -H "Authorization: Bearer $TASKFLOW_BROKER_SECRET" \
  http://127.0.0.1:8089/tasks/<task_id>/chairman-remind
curl -sS -X POST -H "Authorization: Bearer $TASKFLOW_BROKER_SECRET" \
  http://127.0.0.1:8089/tasks/batch/<batch_id>/chairman-remind
```

Автоматические напоминания брокера: просрочка по `due_at` и периодические «мягкие» напоминания **только** если до дедлайна ≥ `LONG_TASK_REMINDER_MIN_HOURS` часов от момента отправки; отправка только в том же окне 8–19.

Сформируй краткую сводку для руководителя.

## Блок E — секреты

Никогда не вставляй в ответ пользователя значения Bearer-токена или bot token. Если пользователь присылает токен в чат — напомни отозвать его в @BotFather.

## Настройка на VDS

В профиле пользователя, от которого запущен OpenClaw Gateway (или в unit `Environment=`):

```bash
export TASKFLOW_BROKER_SECRET='same_as_BROKER_API_SECRET'
```

При смене порта поправь URL в командах выше.
