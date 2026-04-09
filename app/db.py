from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from app.config import settings


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
              reminder_sent   INTEGER NOT NULL DEFAULT 0
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
            """
        )
        con.commit()


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
) -> str:
    tid = str(uuid.uuid4())
    now = _utc_now()
    con.execute(
        """
        INSERT INTO tasks (id, title, body, assignee_tg_id, due_at, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (tid, title, body or "", assignee_tg_id, due_at, now),
    )
    event(con, tid, "created", {"assignee_tg_id": assignee_tg_id, "due_at": due_at})
    con.commit()
    return tid


def set_task_sent(con: sqlite3.Connection, task_id: str) -> None:
    con.execute("UPDATE tasks SET status = 'sent' WHERE id = ?", (task_id,))
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
    con.execute(
        "UPDATE tasks SET status = 'done', completed_note = ?, reminder_sent = 1 WHERE id = ?",
        (note, tid),
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
