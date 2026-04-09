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

## Блок B — создание поручения

1. `curl -sS -H "Authorization: Bearer $TASKFLOW_BROKER_SECRET" http://127.0.0.1:8089/contacts` — найди исполнителя по ФИО (`full_name`) или `tg_user_id`; при неоднозначности спроси руководителя.
2. Создай задачу (ровно одно из полей `assignee_tg_id` или `assignee_name`):

```bash
curl -sS -H "Authorization: Bearer $TASKFLOW_BROKER_SECRET" -H "Content-Type: application/json" \
  -d '{"assignee_name":"Иванов И.И.","title":"...","body":"...","due_at":"2026-04-10T15:00:00Z"}' \
  http://127.0.0.1:8089/tasks
```

3. Ответь пользователю: `task_id`, кому ушло, срок, краткий текст поручения. При HTTP 409 `ambiguous_name` — перечисли варианты из `matches`. При ошибке — кратко статус и тело ответа.

## Блок C — статус и отчёты

```bash
curl -sS -H "Authorization: Bearer $TASKFLOW_BROKER_SECRET" "http://127.0.0.1:8089/tasks"
curl -sS -H "Authorization: Bearer $TASKFLOW_BROKER_SECRET" "http://127.0.0.1:8089/tasks?status=sent"
curl -sS -H "Authorization: Bearer $TASKFLOW_BROKER_SECRET" "http://127.0.0.1:8089/tasks/<task_id>"
```

Сформируй краткую сводку для руководителя.

## Блок D — секреты

Никогда не вставляй в ответ пользователя значения Bearer-токена или bot token. Если пользователь присылает токен в чат — напомни отозвать его в @BotFather.

## Настройка на VDS

В профиле пользователя, от которого запущен OpenClaw Gateway (или в unit `Environment=`):

```bash
export TASKFLOW_BROKER_SECRET='same_as_BROKER_API_SECRET'
```

При смене порта поправь URL в командах выше.
