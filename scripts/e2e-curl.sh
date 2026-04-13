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

echo "== create task — JSON only (no files) =="
ASSIGNEE="${ASSIGNEE_TG_ID:?export ASSIGNEE_TG_ID=...}"
curl -sS -H "${H}" -H "Content-Type: application/json" \
  -d "{\"assignee_tg_id\":\"${ASSIGNEE}\",\"title\":\"E2E test\",\"body\":\"curl\",\"due_at\":\"2099-01-01T12:00:00Z\"}" \
  "${BASE}/tasks/json" | jq .

echo "== create task — multipart with file attachment =="
curl -sS -H "${H}" \
  -F "assignee_tg_id=${ASSIGNEE}" \
  -F "title=E2E test with file" \
  -F "body=see attached" \
  -F "due_at=2099-01-01T12:00:00Z" \
  -F "files=@/tmp/test.txt" \
  "${BASE}/tasks" | jq .

echo "== list tasks =="
curl -sS -H "${H}" "${BASE}/tasks" | jq .

echo "== upload extra attachment to a task =="
echo "(set TASK_ID from the create response above)"
# curl -sS -H "${H}" -F "file=@/tmp/report.pdf" "${BASE}/tasks/${TASK_ID}/attachments" | jq .

echo "== list attachments for a task =="
# curl -sS -H "${H}" "${BASE}/tasks/${TASK_ID}/attachments" | jq .

echo "== download an attachment =="
# curl -sS -H "${H}" -o /tmp/downloaded.pdf "${BASE}/attachments/${ATTACHMENT_ID}"

echo "Then: employee sends «готово» or /done in staff bot (can attach docs/photos)."
echo "Chairman receives completion message + forwarded files."
echo "Finally: GET /tasks?status=done"
