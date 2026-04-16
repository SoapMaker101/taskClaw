from __future__ import annotations

import csv
import io
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from app.config import settings


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _upload_dir() -> Path:
    p = Path(settings.upload_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def init_db() -> None:
    path = Path(settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS contacts (
              tg_user_id   TEXT PRIMARY KEY,
              full_name    TEXT NOT NULL,
              username     TEXT,
              created_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS contact_groups (
              id         TEXT PRIMARY KEY,
              name       TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS contact_group_members (
              group_id    TEXT NOT NULL REFERENCES contact_groups(id) ON DELETE CASCADE,
              tg_user_id  TEXT NOT NULL REFERENCES contacts(tg_user_id) ON DELETE CASCADE,
              PRIMARY KEY (group_id, tg_user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_group_members_user ON contact_group_members(tg_user_id);

            CREATE TABLE IF NOT EXISTS tasks (
              id              TEXT PRIMARY KEY,
              title           TEXT NOT NULL,
              body            TEXT,
              assignee_tg_id  TEXT NOT NULL REFERENCES contacts(tg_user_id),
              due_at          TEXT,
              status          TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'sent', 'done', 'cancelled')),
              created_at      TEXT NOT NULL,
              completed_note  TEXT,
              completed_at    TEXT,
              reminder_sent   INTEGER NOT NULL DEFAULT 0,
              sent_at         TEXT,
              idle_nudges_sent INTEGER NOT NULL DEFAULT 0,
              last_idle_nudge_at TEXT,
              source_group_id TEXT REFERENCES contact_groups(id),
              batch_id        TEXT
            );

            CREATE TABLE IF NOT EXISTS task_events (
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
              type       TEXT NOT NULL,
              payload    TEXT,
              created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_tg_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

            CREATE TABLE IF NOT EXISTS task_attachments (
              id            TEXT PRIMARY KEY,
              task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
              file_name     TEXT NOT NULL,
              stored_path   TEXT NOT NULL,
              mime_type     TEXT,
              size_bytes    INTEGER,
              uploaded_by   TEXT,
              phase         TEXT NOT NULL DEFAULT 'creation'
                CHECK (phase IN ('creation', 'completion')),
              created_at    TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_attachments_task ON task_attachments(task_id);
            """
        )
        con.commit()
        _migrate_tasks_reminder_columns(con)
        _migrate_groups_and_task_columns(con)
        con.commit()


def _migrate_tasks_reminder_columns(con: sqlite3.Connection) -> None:
    """Add columns introduced after first deploy (SQLite has no IF NOT EXISTS for columns)."""
    rows = con.execute("PRAGMA table_info(tasks)").fetchall()
    cols = {str(r[1]) for r in rows}
    alters: list[str] = []
    if "sent_at" not in cols:
        alters.append("ALTER TABLE tasks ADD COLUMN sent_at TEXT")
    if "idle_nudges_sent" not in cols:
        alters.append("ALTER TABLE tasks ADD COLUMN idle_nudges_sent INTEGER NOT NULL DEFAULT 0")
    if "last_idle_nudge_at" not in cols:
        alters.append("ALTER TABLE tasks ADD COLUMN last_idle_nudge_at TEXT")
    for stmt in alters:
        con.execute(stmt)
    # Backfill sent_at for already-delivered tasks (best-effort).
    con.execute(
        """
        UPDATE tasks SET sent_at = created_at
        WHERE status = 'sent' AND (sent_at IS NULL OR TRIM(COALESCE(sent_at, '')) = '')
        """
    )


def _migrate_groups_and_task_columns(con: sqlite3.Connection) -> None:
    """contact_groups / members + tasks.source_group_id, batch_id, completed_at."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_groups (
          id         TEXT PRIMARY KEY,
          name       TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_group_members (
          group_id    TEXT NOT NULL REFERENCES contact_groups(id) ON DELETE CASCADE,
          tg_user_id  TEXT NOT NULL REFERENCES contacts(tg_user_id) ON DELETE CASCADE,
          PRIMARY KEY (group_id, tg_user_id)
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_group_members_user ON contact_group_members(tg_user_id)"
    )
    rows = con.execute("PRAGMA table_info(tasks)").fetchall()
    cols = {str(r[1]) for r in rows}
    for col, stmt in (
        ("completed_at", "ALTER TABLE tasks ADD COLUMN completed_at TEXT"),
        ("source_group_id", "ALTER TABLE tasks ADD COLUMN source_group_id TEXT"),
        ("batch_id", "ALTER TABLE tasks ADD COLUMN batch_id TEXT"),
    ):
        if col not in cols:
            con.execute(stmt)


def _parse_iso_utc(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        s = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    con = sqlite3.connect(settings.database_path)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


def event(con: sqlite3.Connection, task_id: str, typ: str, payload: Optional[dict] = None) -> None:
    con.execute(
        "INSERT INTO task_events (task_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (task_id, typ, json.dumps(payload) if payload else None, _utc_now()),
    )


def list_contacts(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = con.execute("SELECT tg_user_id, full_name, username, created_at FROM contacts ORDER BY full_name").fetchall()
    return [dict(r) for r in rows]


def upsert_contact(con: sqlite3.Connection, tg_user_id: str, full_name: str, username: Optional[str]) -> None:
    con.execute(
        """
        INSERT INTO contacts (tg_user_id, full_name, username, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(tg_user_id) DO UPDATE SET
          full_name = excluded.full_name,
          username = excluded.username
        """,
        (tg_user_id, full_name, username or "", _utc_now()),
    )


def create_task_and_send(
    con: sqlite3.Connection,
    assignee_tg_id: str,
    title: str,
    body: Optional[str],
    due_at: Optional[str],
    source_group_id: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> str:
    tid = str(uuid.uuid4())
    now = _utc_now()
    con.execute(
        """
        INSERT INTO tasks (
          id, title, body, assignee_tg_id, due_at, status, created_at,
          source_group_id, batch_id
        )
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """,
        (tid, title, body or "", assignee_tg_id, due_at, now, source_group_id, batch_id),
    )
    event(
        con,
        tid,
        "created",
        {
            "assignee_tg_id": assignee_tg_id,
            "due_at": due_at,
            "source_group_id": source_group_id,
            "batch_id": batch_id,
        },
    )
    con.commit()
    return tid


def set_task_sent(con: sqlite3.Connection, task_id: str) -> None:
    now = _utc_now()
    con.execute(
        "UPDATE tasks SET status = 'sent', sent_at = ? WHERE id = ?",
        (now, task_id),
    )
    event(con, task_id, "sent", None)
    con.commit()


def list_tasks(con: sqlite3.Connection, status: Optional[str] = None) -> list[dict[str, Any]]:
    if status:
        rows = con.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY datetime(created_at) DESC",
            (status,),
        ).fetchall()
    else:
        rows = con.execute("SELECT * FROM tasks ORDER BY datetime(created_at) DESC").fetchall()
    return [dict(r) for r in rows]


def get_task(con: sqlite3.Connection, task_id: str) -> Optional[dict[str, Any]]:
    row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def complete_latest_open_task(con: sqlite3.Connection, assignee_tg_id: str, note: str) -> Optional[dict[str, Any]]:
    row = con.execute(
        """
        SELECT * FROM tasks
        WHERE assignee_tg_id = ? AND status = 'sent'
        ORDER BY datetime(created_at) DESC LIMIT 1
        """,
        (assignee_tg_id,),
    ).fetchone()
    if not row:
        return None
    tid = row["id"]
    done_at = _utc_now()
    con.execute(
        """
        UPDATE tasks SET status = 'done', completed_note = ?, reminder_sent = 1, completed_at = ?
        WHERE id = ?
        """,
        (note, done_at, tid),
    )
    event(con, tid, "done", {"note": note})
    con.commit()
    base = dict(row)
    return {**base, "completed_note": note, "status": "done"}


def tasks_needing_reminder(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Due in the past (UTC), still sent, not reminded yet."""
    rows = con.execute(
        """
        SELECT * FROM tasks
        WHERE status = 'sent' AND reminder_sent = 0 AND due_at IS NOT NULL
        """
    ).fetchall()
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for r in rows:
        raw = r["due_at"]
        try:
            s = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
            d = datetime.fromisoformat(s)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            if d < now:
                out.append(dict(r))
        except (TypeError, ValueError):
            continue
    return out


def contact_exists(con: sqlite3.Connection, tg_id: str) -> bool:
    return (
        con.execute("SELECT 1 FROM contacts WHERE tg_user_id = ?", (tg_id,)).fetchone() is not None
    )


def search_contacts_by_name(con: sqlite3.Connection, name: str) -> list[dict[str, Any]]:
    fragment = name.strip().lower()
    rows = con.execute(
        "SELECT * FROM contacts WHERE lower(full_name) LIKE ? ORDER BY full_name",
        (f"%{fragment}%",),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_reminder_sent(con: sqlite3.Connection, task_id: str) -> None:
    con.execute("UPDATE tasks SET reminder_sent = 1 WHERE id = ?", (task_id,))
    event(con, task_id, "reminder_sent", None)
    con.commit()


def tasks_needing_idle_nudge(
    con: sqlite3.Connection,
    interval_hours: int,
    max_nudges: int,
    min_span_hours: int = 72,
) -> list[dict[str, Any]]:
    """
    Open tasks (status=sent) that deserve a gentle follow-up before the deadline.
    Only tasks where (due_at - sent_at) >= min_span_hours (default 72) get pre-deadline nudges.
    Without due_at, no pre-deadline nudges (cannot verify span).
    Skips overdue rows (handled by overdue reminder path).
    """
    if max_nudges <= 0 or interval_hours <= 0:
        return []
    rows = con.execute(
        """
        SELECT * FROM tasks
        WHERE status = 'sent' AND idle_nudges_sent < ?
        """,
        (max_nudges,),
    ).fetchall()
    now = datetime.now(timezone.utc)
    delta = timedelta(hours=interval_hours)
    min_span = timedelta(hours=max(0, min_span_hours))
    out: list[dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        due_raw = row.get("due_at")
        due_dt = _parse_iso_utc(due_raw) if due_raw else None
        if due_dt is None:
            continue
        if due_dt < now:
            continue
        base_raw = row.get("sent_at") or row.get("created_at")
        base_dt = _parse_iso_utc(base_raw)
        if base_dt is None:
            continue
        if due_dt - base_dt < min_span:
            continue
        last_raw = row.get("last_idle_nudge_at")
        last_dt = _parse_iso_utc(last_raw) if last_raw else None
        next_at = base_dt + delta if last_dt is None else last_dt + delta
        if now >= next_at:
            out.append(row)
    return out


def mark_idle_nudge_sent(con: sqlite3.Connection, task_id: str) -> None:
    now = _utc_now()
    con.execute(
        """
        UPDATE tasks
        SET idle_nudges_sent = idle_nudges_sent + 1,
            last_idle_nudge_at = ?
        WHERE id = ?
        """,
        (now, task_id),
    )
    event(con, task_id, "idle_reminder_sent", None)
    con.commit()


# ── Contact groups ───────────────────────────────────────────────────────


def get_contact_group(con: sqlite3.Connection, group_id: str) -> Optional[dict[str, Any]]:
    row = con.execute("SELECT * FROM contact_groups WHERE id = ?", (group_id,)).fetchone()
    if not row:
        return None
    base = dict(row)
    members = con.execute(
        """
        SELECT c.tg_user_id, c.full_name, c.username
        FROM contact_group_members m
        JOIN contacts c ON c.tg_user_id = m.tg_user_id
        WHERE m.group_id = ?
        ORDER BY c.full_name
        """,
        (group_id,),
    ).fetchall()
    base["members"] = [dict(m) for m in members]
    return base


def list_contact_groups(con: sqlite3.Connection) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in con.execute("SELECT id FROM contact_groups ORDER BY name").fetchall():
        g = get_contact_group(con, str(r["id"]))
        if g:
            out.append(g)
    return out


def insert_contact_group(con: sqlite3.Connection, name: str, member_tg_ids: list[str]) -> str:
    gid = str(uuid.uuid4())
    now = _utc_now()
    con.execute(
        "INSERT INTO contact_groups (id, name, created_at) VALUES (?, ?, ?)",
        (gid, name.strip(), now),
    )
    for uid in dict.fromkeys(member_tg_ids):
        con.execute(
            "INSERT INTO contact_group_members (group_id, tg_user_id) VALUES (?, ?)",
            (gid, uid),
        )
    con.commit()
    return gid


def patch_group_members(
    con: sqlite3.Connection,
    group_id: str,
    add_tg_ids: list[str],
    remove_tg_ids: list[str],
) -> None:
    for uid in add_tg_ids:
        con.execute(
            "INSERT OR IGNORE INTO contact_group_members (group_id, tg_user_id) VALUES (?, ?)",
            (group_id, uid),
        )
    for uid in remove_tg_ids:
        con.execute(
            "DELETE FROM contact_group_members WHERE group_id = ? AND tg_user_id = ?",
            (group_id, uid),
        )
    con.commit()


def group_member_tg_ids(con: sqlite3.Connection, group_id: str) -> list[str]:
    rows = con.execute(
        "SELECT tg_user_id FROM contact_group_members WHERE group_id = ? ORDER BY tg_user_id",
        (group_id,),
    ).fetchall()
    return [str(r["tg_user_id"]) for r in rows]


def list_sent_tasks_by_batch_id(con: sqlite3.Connection, batch_id: str) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT * FROM tasks
        WHERE batch_id = ? AND status = 'sent'
        ORDER BY assignee_tg_id
        """,
        (batch_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def tasks_for_summary_scope(
    con: sqlite3.Connection,
    scope: str,
    assignee_tg_id: Optional[str],
    group_id: Optional[str],
) -> list[dict[str, Any]]:
    if scope == "all":
        q = """
            SELECT t.*, c.full_name AS assignee_name
            FROM tasks t
            LEFT JOIN contacts c ON c.tg_user_id = t.assignee_tg_id
            ORDER BY datetime(t.created_at) DESC
        """
        rows = con.execute(q).fetchall()
    elif scope == "user":
        if not assignee_tg_id:
            return []
        q = """
            SELECT t.*, c.full_name AS assignee_name
            FROM tasks t
            LEFT JOIN contacts c ON c.tg_user_id = t.assignee_tg_id
            WHERE t.assignee_tg_id = ?
            ORDER BY datetime(t.created_at) DESC
        """
        rows = con.execute(q, (assignee_tg_id,)).fetchall()
    elif scope == "group":
        if not group_id:
            return []
        q = """
            SELECT t.*, c.full_name AS assignee_name
            FROM tasks t
            LEFT JOIN contacts c ON c.tg_user_id = t.assignee_tg_id
            WHERE t.source_group_id = ?
               OR t.assignee_tg_id IN (
                 SELECT tg_user_id FROM contact_group_members WHERE group_id = ?
               )
            ORDER BY datetime(t.created_at) DESC
        """
        rows = con.execute(q, (group_id, group_id)).fetchall()
    else:
        return []
    return [dict(r) for r in rows]


def build_tasks_summary_csv(
    con: sqlite3.Connection,
    scope: str,
    assignee_tg_id: Optional[str],
    group_id: Optional[str],
) -> str:
    """UTF-8 text; caller may add BOM for Excel."""
    rows = tasks_for_summary_scope(con, scope, assignee_tg_id, group_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "section",
            "id",
            "title",
            "status",
            "assignee_tg_id",
            "assignee_name",
            "due_at",
            "created_at",
            "completed_at",
            "source_group_id",
            "batch_id",
        ]
    )

    def write_block(section: str, subset: list[dict[str, Any]]) -> None:
        for r in subset:
            w.writerow(
                [
                    section,
                    r.get("id"),
                    r.get("title"),
                    r.get("status"),
                    r.get("assignee_tg_id"),
                    r.get("assignee_name") or "",
                    r.get("due_at") or "",
                    r.get("created_at") or "",
                    r.get("completed_at") or "",
                    r.get("source_group_id") or "",
                    r.get("batch_id") or "",
                ]
            )

    write_block("awaiting_delivery", [r for r in rows if r["status"] == "pending"])
    write_block("in_progress", [r for r in rows if r["status"] == "sent"])
    write_block("completed", [r for r in rows if r["status"] == "done"])
    write_block("cancelled", [r for r in rows if r["status"] == "cancelled"])
    return buf.getvalue()


def build_tasks_summary_md(
    con: sqlite3.Connection,
    scope: str,
    assignee_tg_id: Optional[str],
    group_id: Optional[str],
) -> str:
    rows = tasks_for_summary_scope(con, scope, assignee_tg_id, group_id)

    def lines_for(label: str, subset: list[dict[str, Any]]) -> list[str]:
        if not subset:
            return [f"## {label}", "_нет_", ""]
        out = [f"## {label}", ""]
        for r in subset:
            name = r.get("assignee_name") or ""
            out.append(
                f"- **{r.get('id')}** — {r.get('title')} — `{r.get('status')}` — "
                f"{name} (tg {r.get('assignee_tg_id')}) — срок: {r.get('due_at') or '—'}"
            )
        out.append("")
        return out

    parts = ["# Свод по задачам", ""]
    parts += lines_for("Ожидают доставки (pending)", [r for r in rows if r["status"] == "pending"])
    parts += lines_for("В работе (sent)", [r for r in rows if r["status"] == "sent"])
    parts += lines_for("Выполнено (done)", [r for r in rows if r["status"] == "done"])
    parts += lines_for("Отменено (cancelled)", [r for r in rows if r["status"] == "cancelled"])
    return "\n".join(parts).rstrip() + "\n"


# ── Attachments ──────────────────────────────────────────────────────────


def save_attachment(
    con: sqlite3.Connection,
    task_id: str,
    file_name: str,
    data: bytes,
    mime_type: Optional[str] = None,
    uploaded_by: Optional[str] = None,
    phase: str = "creation",
) -> dict[str, Any]:
    att_id = str(uuid.uuid4())
    ext = Path(file_name).suffix or ""
    stored_name = f"{att_id}{ext}"
    dest = _upload_dir() / stored_name
    dest.write_bytes(data)

    con.execute(
        """
        INSERT INTO task_attachments
          (id, task_id, file_name, stored_path, mime_type, size_bytes, uploaded_by, phase, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (att_id, task_id, file_name, stored_name, mime_type, len(data), uploaded_by, phase, _utc_now()),
    )
    event(con, task_id, "attachment_added", {"attachment_id": att_id, "file_name": file_name, "phase": phase})
    con.commit()
    return {
        "id": att_id,
        "task_id": task_id,
        "file_name": file_name,
        "stored_path": stored_name,
        "mime_type": mime_type,
        "size_bytes": len(data),
        "phase": phase,
    }


def list_attachments(con: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT id, task_id, file_name, mime_type, size_bytes, uploaded_by, phase, created_at "
        "FROM task_attachments WHERE task_id = ? ORDER BY created_at",
        (task_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_attachment(con: sqlite3.Connection, attachment_id: str) -> Optional[dict[str, Any]]:
    row = con.execute("SELECT * FROM task_attachments WHERE id = ?", (attachment_id,)).fetchone()
    return dict(row) if row else None


def attachment_fs_path(stored_path: str) -> Path:
    return _upload_dir() / stored_path


def delete_attachment(con: sqlite3.Connection, attachment_id: str) -> bool:
    row = con.execute("SELECT stored_path, task_id FROM task_attachments WHERE id = ?", (attachment_id,)).fetchone()
    if not row:
        return False
    fp = attachment_fs_path(row["stored_path"])
    if fp.exists():
        fp.unlink()
    con.execute("DELETE FROM task_attachments WHERE id = ?", (attachment_id,))
    event(con, row["task_id"], "attachment_removed", {"attachment_id": attachment_id})
    con.commit()
    return True
