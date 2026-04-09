#!/usr/bin/env bash
# Manual E2E checklist on VDS (after staff user registered via /start + ФИО).
# Set BROKER_URL, BROKER_API_SECRET, assignee_tg_id from GET /contacts.
set -euo pipefail
BASE="${BROKER_URL:-http://127.0.0.1:8089}"
SECRET="${BROKER_API_SECRET:?set BROKER_API_SECRET}"
H="Authorization: Bearer ${SECRET}"

echo "== health (no auth) =="
curl -sS "${BASE}/health" | jq .

echo "== contacts =="
curl -sS -H "${H}" "${BASE}/contacts" | jq .

echo "== create task (set ASSIGNEE_TG_ID) =="
ASSIGNEE="${ASSIGNEE_TG_ID:?export ASSIGNEE_TG_ID=...}"
curl -sS -H "${H}" -H "Content-Type: application/json" \
  -d "{\"assignee_tg_id\":\"${ASSIGNEE}\",\"title\":\"E2E test\",\"body\":\"curl\",\"due_at\":\"2099-01-01T12:00:00Z\"}" \
  "${BASE}/tasks" | jq .

echo "== list tasks =="
curl -sS -H "${H}" "${BASE}/tasks" | jq .

echo "Then: employee sends «готово» or /done in staff bot; chairman should get Telegram from personal bot."
echo "Finally: GET /tasks?status=done (add query in OpenAPI or use jq filter locally)."
