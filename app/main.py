from __future__ import annotations

import asyncio
import logging
import sqlite3
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BufferedInputFile
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from starlette.background import BackgroundTask
from uvicorn import Config, Server

from app import db
from app.config import settings
from app.notify import notify_chairman
from app.staff_handlers import router as staff_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def verify_bearer(authorization: Annotated[Optional[str], Header()] = None) -> None:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization")
    token = authorization[7:].strip()
    if token != settings.broker_api_secret:
        raise HTTPException(status_code=401, detail="Invalid token")


def in_reminder_send_window() -> bool:
    """Local wall time in [start_hour, end_hour), e.g. 08:00–18:59 (19:00 excluded)."""
    try:
        tz = ZoneInfo(settings.reminder_timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    h = now.hour
    return settings.reminder_window_start_hour <= h < settings.reminder_window_end_hour


def _reminder_window_human_hint() -> str:
    return (
        f"Напоминания отправляются только с {settings.reminder_window_start_hour:02d}:00 "
        f"до {settings.reminder_window_end_hour:02d}:00 по часовому поясу {settings.reminder_timezone} "
        "(конец интервала не включительно)."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    bot = Bot(settings.staff_bot_token, default=DefaultBotProperties())
    dp = Dispatcher()
    dp.include_router(staff_router)
    app.state.staff_bot = bot

    async def reminder_loop() -> None:
        while True:
            await asyncio.sleep(max(15, settings.reminder_poll_seconds))
            with db.get_conn() as con:
                due = db.tasks_needing_reminder(con)
            if not in_reminder_send_window():
                due = []
            for t in due:
                tid = t["id"]
                assignee = int(t["assignee_tg_id"])
                title = t["title"]
                due_at = t["due_at"] or ""
                text = f"Напоминание: просрочено поручение «{title}». Срок был: {due_at}"
                try:
                    await bot.send_message(assignee, text)
                    with db.get_conn() as con:
                        db.mark_reminder_sent(con, tid)
                except Exception:
                    logger.exception("reminder to %s failed", assignee)

            with db.get_conn() as con:
                idle = db.tasks_needing_idle_nudge(
                    con,
                    settings.idle_reminder_interval_hours,
                    settings.idle_reminder_max_per_task,
                    settings.long_task_reminder_min_hours,
                )
            if not in_reminder_send_window():
                idle = []
            for t in idle:
                tid = t["id"]
                assignee = int(t["assignee_tg_id"])
                title = t["title"]
                due_line = ""
                raw_due = t.get("due_at")
                if raw_due:
                    due_line = f"\nСрок: {raw_due}"
                text = (
                    f"Напоминание: у вас всё ещё открыто поручение «{title}».{due_line}\n\n"
                    f"Ответьте «готово» или /done, когда выполните."
                )
                try:
                    await bot.send_message(assignee, text)
                    with db.get_conn() as con:
                        db.mark_idle_nudge_sent(con, tid)
                except Exception:
                    logger.exception("idle reminder to %s failed", assignee)

    poll_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    remind_task = asyncio.create_task(reminder_loop())
    logger.info("Staff bot polling started on %s:%s", settings.broker_host, settings.broker_port)
    try:
        yield
    finally:
        remind_task.cancel()
        poll_task.cancel()
        try:
            await remind_task
        except asyncio.CancelledError:
            pass
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()


app = FastAPI(title="Task Broker", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/contacts", dependencies=[Depends(verify_bearer)])
async def api_contacts() -> dict:
    with db.get_conn() as con:
        return {"contacts": db.list_contacts(con)}


class GroupCreate(BaseModel):
    name: str
    member_tg_ids: list[str] = Field(default_factory=list)


class GroupPatch(BaseModel):
    add_member_tg_ids: list[str] = Field(default_factory=list)
    remove_member_tg_ids: list[str] = Field(default_factory=list)


class TaskCreate(BaseModel):
    assignee_tg_id: Optional[str] = None
    assignee_name: Optional[str] = None
    assignee_group_id: Optional[str] = None
    title: str
    body: Optional[str] = None
    due_at: Optional[str] = None

    @model_validator(mode="after")
    def exactly_one_assignee(self) -> TaskCreate:
        has_id = bool(self.assignee_tg_id and str(self.assignee_tg_id).strip())
        has_name = bool(self.assignee_name and self.assignee_name.strip())
        has_group = bool(self.assignee_group_id and str(self.assignee_group_id).strip())
        if int(has_id) + int(has_name) + int(has_group) != 1:
            raise ValueError("Provide exactly one of assignee_tg_id, assignee_name, assignee_group_id")
        return self


@app.get("/groups", dependencies=[Depends(verify_bearer)])
async def api_list_groups() -> dict:
    with db.get_conn() as con:
        return {"groups": db.list_contact_groups(con)}


@app.get("/groups/{group_id}", dependencies=[Depends(verify_bearer)])
async def api_get_group(group_id: str) -> dict:
    with db.get_conn() as con:
        g = db.get_contact_group(con, group_id)
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        return {"group": g}


@app.post("/groups", dependencies=[Depends(verify_bearer)])
async def api_create_group(body: GroupCreate) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name is required")
    if len(body.member_tg_ids) > settings.max_group_members:
        raise HTTPException(status_code=400, detail="Too many members for one request")
    with db.get_conn() as con:
        for uid in body.member_tg_ids:
            if not db.contact_exists(con, uid):
                raise HTTPException(status_code=400, detail=f"Unknown contact tg id: {uid}")
        try:
            gid = db.insert_contact_group(con, name, body.member_tg_ids)
        except sqlite3.IntegrityError as e:
            raise HTTPException(status_code=409, detail=f"Group name conflict or invalid data: {e}") from e
        g = db.get_contact_group(con, gid)
    return {"group": g}


@app.patch("/groups/{group_id}", dependencies=[Depends(verify_bearer)])
async def api_patch_group(group_id: str, body: GroupPatch) -> dict:
    with db.get_conn() as con:
        if not db.get_contact_group(con, group_id):
            raise HTTPException(status_code=404, detail="Group not found")
        for uid in body.add_member_tg_ids:
            if not db.contact_exists(con, uid):
                raise HTTPException(status_code=400, detail=f"Unknown contact tg id: {uid}")
        cur = set(db.group_member_tg_ids(con, group_id))
        for u in body.remove_member_tg_ids:
            cur.discard(u)
        for u in body.add_member_tg_ids:
            cur.add(u)
        if len(cur) > settings.max_group_members:
            raise HTTPException(status_code=400, detail="Group would exceed max_group_members")
        db.patch_group_members(con, group_id, body.add_member_tg_ids, body.remove_member_tg_ids)
        g = db.get_contact_group(con, group_id)
    return {"group": g}


def _resolve_assignee(con: sqlite3.Connection, body: TaskCreate) -> str:
    if body.assignee_tg_id:
        tid = body.assignee_tg_id.strip()
        if not db.contact_exists(con, tid):
            raise HTTPException(status_code=404, detail=f"Unknown assignee tg id: {tid}")
        return tid
    if body.assignee_name:
        matches = db.search_contacts_by_name(con, body.assignee_name)
        if not matches:
            raise HTTPException(status_code=404, detail="No contact matches name")
        if len(matches) > 1:
            names = [m["full_name"] for m in matches]
            raise HTTPException(status_code=409, detail={"error": "ambiguous_name", "matches": names})
        return str(matches[0]["tg_user_id"])
    raise HTTPException(status_code=500, detail="Invalid task assignee state")


def _resolve_group_assignees(con: sqlite3.Connection, group_id: str) -> list[str]:
    gid = group_id.strip()
    if not db.get_contact_group(con, gid):
        raise HTTPException(status_code=404, detail="Unknown assignee_group_id")
    members = db.group_member_tg_ids(con, gid)
    if not members:
        raise HTTPException(status_code=400, detail="Group has no members")
    if len(members) > settings.max_group_members:
        raise HTTPException(status_code=400, detail="Group exceeds max_group_members")
    return members


async def _telegram_deliver_task(
    bot: Bot,
    assignee_tg_id: str,
    title: str,
    body: Optional[str],
    due_at: Optional[str],
    saved_attachments: list[dict],
) -> None:
    due_line = f"\nСрок: {due_at}" if due_at else ""
    msg = f"Поручение\n{title}\n{body or ''}{due_line}\n\nОтветьте «готово» или /done когда выполните."
    await bot.send_message(int(assignee_tg_id), msg)
    for att in saved_attachments:
        fp = db.attachment_fs_path(att["stored_path"])
        buf = BufferedInputFile(fp.read_bytes(), filename=att["file_name"])
        await bot.send_document(int(assignee_tg_id), buf)


_MAX_BYTES = settings.max_file_size_mb * 1024 * 1024


async def _read_upload(f: UploadFile) -> bytes:
    data = await f.read()
    if len(data) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {settings.max_file_size_mb} MB)")
    return data


@app.post("/tasks", dependencies=[Depends(verify_bearer)])
async def api_create_task(
    title: Annotated[str, Form()],
    assignee_tg_id: Annotated[Optional[str], Form()] = None,
    assignee_name: Annotated[Optional[str], Form()] = None,
    assignee_group_id: Annotated[Optional[str], Form()] = None,
    body: Annotated[Optional[str], Form()] = None,
    due_at: Annotated[Optional[str], Form()] = None,
    files: list[UploadFile] = File(default=[]),
) -> dict:
    try:
        tc = TaskCreate(
            assignee_tg_id=assignee_tg_id,
            assignee_name=assignee_name,
            assignee_group_id=assignee_group_id,
            title=title,
            body=body,
            due_at=due_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    bot: Bot = app.state.staff_bot

    if tc.assignee_group_id:
        with db.get_conn() as con:
            members = _resolve_group_assignees(con, tc.assignee_group_id)
            group_key = tc.assignee_group_id.strip()
            batch_id = str(uuid.uuid4())
        file_payload: list[tuple[str, bytes, Optional[str]]] = []
        for f in files:
            data = await _read_upload(f)
            file_payload.append((f.filename or "file", data, f.content_type))
        results: list[dict] = []
        for assignee in members:
            with db.get_conn() as con:
                task_id = db.create_task_and_send(
                    con,
                    assignee,
                    tc.title,
                    tc.body,
                    tc.due_at,
                    source_group_id=group_key,
                    batch_id=batch_id,
                )
            saved: list[dict] = []
            for fname, data, ctype in file_payload:
                with db.get_conn() as con:
                    att = db.save_attachment(
                        con,
                        task_id,
                        fname,
                        data,
                        mime_type=ctype,
                        uploaded_by="api",
                        phase="creation",
                    )
                saved.append(att)
            try:
                await _telegram_deliver_task(bot, assignee, tc.title, tc.body, tc.due_at, saved)
            except Exception as e:
                logger.exception("deliver task to staff (group fan-out)")
                raise HTTPException(status_code=502, detail=f"telegram_send_failed: {e!s}") from e
            with db.get_conn() as con:
                db.set_task_sent(con, task_id)
            public = [{k: v for k, v in a.items() if k != "stored_path"} for a in saved]
            results.append({"task_id": task_id, "assignee_tg_id": assignee, "attachments": public})
        return {
            "batch_id": batch_id,
            "assignee_group_id": group_key,
            "status": "sent",
            "tasks": results,
        }

    with db.get_conn() as con:
        assignee = _resolve_assignee(con, tc)
        task_id = db.create_task_and_send(con, assignee, tc.title, tc.body, tc.due_at)

    saved: list[dict] = []
    if files:
        for f in files:
            data = await _read_upload(f)
            with db.get_conn() as con:
                att = db.save_attachment(
                    con,
                    task_id,
                    f.filename or "file",
                    data,
                    mime_type=f.content_type,
                    uploaded_by="api",
                    phase="creation",
                )
            saved.append(att)

    try:
        await _telegram_deliver_task(bot, assignee, tc.title, tc.body, tc.due_at, saved)
    except Exception as e:
        logger.exception("deliver task to staff")
        raise HTTPException(status_code=502, detail=f"telegram_send_failed: {e!s}") from e

    with db.get_conn() as con:
        db.set_task_sent(con, task_id)

    public = [{k: v for k, v in a.items() if k != "stored_path"} for a in saved]
    return {"task_id": task_id, "assignee_tg_id": assignee, "status": "sent", "attachments": public}


@app.post("/tasks/json", dependencies=[Depends(verify_bearer)])
async def api_create_task_json(body: TaskCreate) -> dict:
    """JSON-only task creation (no file attachments)."""
    bot: Bot = app.state.staff_bot

    if body.assignee_group_id:
        with db.get_conn() as con:
            members = _resolve_group_assignees(con, body.assignee_group_id)
            group_key = body.assignee_group_id.strip()
            batch_id = str(uuid.uuid4())
        results: list[dict] = []
        for assignee in members:
            with db.get_conn() as con:
                task_id = db.create_task_and_send(
                    con,
                    assignee,
                    body.title,
                    body.body,
                    body.due_at,
                    source_group_id=group_key,
                    batch_id=batch_id,
                )
            try:
                await _telegram_deliver_task(bot, assignee, body.title, body.body, body.due_at, [])
            except Exception as e:
                logger.exception("deliver task to staff (group fan-out)")
                raise HTTPException(status_code=502, detail=f"telegram_send_failed: {e!s}") from e
            with db.get_conn() as con:
                db.set_task_sent(con, task_id)
            results.append({"task_id": task_id, "assignee_tg_id": assignee})
        return {
            "batch_id": batch_id,
            "assignee_group_id": group_key,
            "status": "sent",
            "tasks": results,
        }

    with db.get_conn() as con:
        assignee = _resolve_assignee(con, body)
        task_id = db.create_task_and_send(con, assignee, body.title, body.body, body.due_at)

    try:
        await _telegram_deliver_task(bot, assignee, body.title, body.body, body.due_at, [])
    except Exception as e:
        logger.exception("deliver task to staff")
        raise HTTPException(status_code=502, detail=f"telegram_send_failed: {e!s}") from e

    with db.get_conn() as con:
        db.set_task_sent(con, task_id)

    return {"task_id": task_id, "assignee_tg_id": assignee, "status": "sent"}


def _require_reminder_window() -> None:
    if not in_reminder_send_window():
        raise HTTPException(status_code=400, detail=_reminder_window_human_hint())


@app.post("/tasks/{task_id}/chairman-remind", dependencies=[Depends(verify_bearer)])
async def api_chairman_remind_task(task_id: str) -> dict:
    _require_reminder_window()
    bot: Bot = app.state.staff_bot
    with db.get_conn() as con:
        t = db.get_task(con, task_id)
        if not t:
            raise HTTPException(status_code=404, detail="Task not found")
        if t["status"] != "sent":
            raise HTTPException(
                status_code=400,
                detail="Chairman reminder is only sent for tasks in status sent",
            )
    title = t["title"]
    text = (
        f"Напоминание от председателя по поручению «{title}».\n"
        "Пожалуйста, ответьте «готово» или /done, когда выполните."
    )
    assignee = int(t["assignee_tg_id"])
    try:
        await bot.send_message(assignee, text)
    except Exception as e:
        logger.exception("chairman remind task")
        raise HTTPException(status_code=502, detail=f"telegram_send_failed: {e!s}") from e
    with db.get_conn() as con:
        db.event(con, task_id, "chairman_reminder", {"scope": "task"})
        con.commit()
    return {"ok": True, "task_id": task_id, "notified_assignees": 1}


@app.post("/tasks/batch/{batch_id}/chairman-remind", dependencies=[Depends(verify_bearer)])
async def api_chairman_remind_batch(batch_id: str) -> dict:
    _require_reminder_window()
    bot: Bot = app.state.staff_bot
    with db.get_conn() as con:
        rows = db.list_sent_tasks_by_batch_id(con, batch_id)
    if not rows:
        raise HTTPException(status_code=404, detail="No open tasks for this batch_id")
    notified = 0
    for t in rows:
        title = t["title"]
        text = (
            f"Напоминание от председателя по поручению «{title}».\n"
            "Пожалуйста, ответьте «готово» или /done, когда выполните."
        )
        assignee = int(t["assignee_tg_id"])
        try:
            await bot.send_message(assignee, text)
            notified += 1
        except Exception:
            logger.exception("chairman remind batch to %s", assignee)
    with db.get_conn() as con:
        for t in rows:
            db.event(con, t["id"], "chairman_reminder", {"scope": "batch", "batch_id": batch_id})
        con.commit()
    try:
        await notify_chairman(
            f"Напоминание по выдаче (batch {batch_id}): сообщение доставлено {notified} исполнителям."
        )
    except Exception:
        logger.exception("chairman ack notify")
    return {"ok": True, "batch_id": batch_id, "notified_assignees": notified}


@app.get("/reports/tasks-summary", dependencies=[Depends(verify_bearer)])
async def api_tasks_summary_report(
    scope: Annotated[str, Query()],
    export_format: Annotated[str, Query(alias="format")] = "csv",
    assignee_tg_id: Annotated[Optional[str], Query()] = None,
    group_id: Annotated[Optional[str], Query()] = None,
):
    if scope not in ("all", "user", "group"):
        raise HTTPException(status_code=400, detail="scope must be all, user, or group")
    if scope == "user" and not (assignee_tg_id and assignee_tg_id.strip()):
        raise HTTPException(status_code=400, detail="assignee_tg_id required for scope=user")
    if scope == "group" and not (group_id and group_id.strip()):
        raise HTTPException(status_code=400, detail="group_id required for scope=group")
    if export_format not in ("csv", "md"):
        raise HTTPException(status_code=400, detail="format must be csv or md")

    with db.get_conn() as con:
        if export_format == "csv":
            raw = db.build_tasks_summary_csv(con, scope, assignee_tg_id, group_id)
            text_data = "\ufeff" + raw
            suffix = ".csv"
            media = "text/csv; charset=utf-8"
        else:
            text_data = db.build_tasks_summary_md(con, scope, assignee_tg_id, group_id)
            suffix = ".md"
            media = "text/markdown; charset=utf-8"

    tmp = Path(tempfile.gettempdir()) / f"task-summary-{uuid.uuid4().hex}{suffix}"
    tmp.write_text(text_data, encoding="utf-8")

    def _unlink(path: str) -> None:
        Path(path).unlink(missing_ok=True)

    return FileResponse(
        path=str(tmp),
        filename=f"tasks-summary-{scope}{suffix}",
        media_type=media,
        background=BackgroundTask(_unlink, str(tmp)),
    )


# ── Attachments CRUD ──────────────────────────────────────────────────


@app.post("/tasks/{task_id}/attachments", dependencies=[Depends(verify_bearer)])
async def api_upload_attachment(
    task_id: str,
    file: UploadFile = File(...),
    phase: Annotated[str, Form()] = "creation",
) -> dict:
    with db.get_conn() as con:
        if not db.get_task(con, task_id):
            raise HTTPException(status_code=404, detail="Task not found")
    data = await _read_upload(file)
    with db.get_conn() as con:
        att = db.save_attachment(
            con, task_id, file.filename or "file", data,
            mime_type=file.content_type, uploaded_by="api", phase=phase,
        )
    return {"attachment": att}


@app.get("/tasks/{task_id}/attachments", dependencies=[Depends(verify_bearer)])
async def api_list_attachments(task_id: str) -> dict:
    with db.get_conn() as con:
        if not db.get_task(con, task_id):
            raise HTTPException(status_code=404, detail="Task not found")
        return {"attachments": db.list_attachments(con, task_id)}


@app.get("/attachments/{attachment_id}", dependencies=[Depends(verify_bearer)])
async def api_download_attachment(attachment_id: str):
    with db.get_conn() as con:
        att = db.get_attachment(con, attachment_id)
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")
    fp = db.attachment_fs_path(att["stored_path"])
    if not fp.exists():
        raise HTTPException(status_code=404, detail="File missing from storage")
    return FileResponse(fp, filename=att["file_name"], media_type=att["mime_type"] or "application/octet-stream")


@app.delete("/attachments/{attachment_id}", dependencies=[Depends(verify_bearer)])
async def api_delete_attachment(attachment_id: str) -> dict:
    with db.get_conn() as con:
        if not db.delete_attachment(con, attachment_id):
            raise HTTPException(status_code=404, detail="Attachment not found")
    return {"deleted": True}


# ── Tasks list / detail ──────────────────────────────────────────────


@app.get("/tasks", dependencies=[Depends(verify_bearer)])
async def api_list_tasks(status: Annotated[Optional[str], Query()] = None) -> dict:
    with db.get_conn() as con:
        return {"tasks": db.list_tasks(con, status)}


@app.get("/tasks/{task_id}", dependencies=[Depends(verify_bearer)])
async def api_get_task(task_id: str) -> dict:
    with db.get_conn() as con:
        row = db.get_task(con, task_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        attachments = db.list_attachments(con, task_id)
    return {"task": row, "attachments": attachments}


def main() -> None:
    """Run API + aiogram in one asyncio loop (production: use uvicorn app.main:app)."""
    async def serve() -> None:
        config = Config(
            app,
            host=settings.broker_host,
            port=settings.broker_port,
            log_level="info",
        )
        server = Server(config)
        await server.serve()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
