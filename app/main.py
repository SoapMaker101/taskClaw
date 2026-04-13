from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated, Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BufferedInputFile
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, model_validator
from uvicorn import Config, Server

from app import db
from app.config import settings
from app.staff_handlers import router as staff_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def verify_bearer(authorization: Annotated[Optional[str], Header()] = None) -> None:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization")
    token = authorization[7:].strip()
    if token != settings.broker_api_secret:
        raise HTTPException(status_code=401, detail="Invalid token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    bot = Bot(settings.staff_bot_token, default=DefaultBotProperties())
    dp = Dispatcher()
    dp.include_router(staff_router)
    app.state.staff_bot = bot

    async def reminder_loop() -> None:
        while True:
            await asyncio.sleep(60)
            with db.get_conn() as con:
                due = db.tasks_needing_reminder(con)
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


class TaskCreate(BaseModel):
    assignee_tg_id: Optional[str] = None
    assignee_name: Optional[str] = None
    title: str
    body: Optional[str] = None
    due_at: Optional[str] = None

    @model_validator(mode="after")
    def exactly_one_assignee(self) -> TaskCreate:
        has_id = bool(self.assignee_tg_id and self.assignee_tg_id.strip())
        has_name = bool(self.assignee_name and self.assignee_name.strip())
        if has_id == has_name:
            raise ValueError("Provide exactly one of assignee_tg_id or assignee_name")
        return self


def _resolve_assignee(con: sqlite3.Connection, body: TaskCreate) -> str:
    if body.assignee_tg_id:
        tid = body.assignee_tg_id.strip()
        if not db.contact_exists(con, tid):
            raise HTTPException(status_code=404, detail=f"Unknown assignee tg id: {tid}")
        return tid
    assert body.assignee_name
    matches = db.search_contacts_by_name(con, body.assignee_name)
    if not matches:
        raise HTTPException(status_code=404, detail="No contact matches name")
    if len(matches) > 1:
        names = [m["full_name"] for m in matches]
        raise HTTPException(status_code=409, detail={"error": "ambiguous_name", "matches": names})
    return str(matches[0]["tg_user_id"])


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
    body: Annotated[Optional[str], Form()] = None,
    due_at: Annotated[Optional[str], Form()] = None,
    files: list[UploadFile] = File(default=[]),
) -> dict:
    tc = TaskCreate(
        assignee_tg_id=assignee_tg_id,
        assignee_name=assignee_name,
        title=title,
        body=body,
        due_at=due_at,
    )
    with db.get_conn() as con:
        assignee = _resolve_assignee(con, tc)
        task_id = db.create_task_and_send(con, assignee, tc.title, tc.body, tc.due_at)

    saved: list[dict] = []
    if files:
        for f in files:
            data = await _read_upload(f)
            with db.get_conn() as con:
                att = db.save_attachment(
                    con, task_id, f.filename or "file", data,
                    mime_type=f.content_type, uploaded_by="api", phase="creation",
                )
            saved.append(att)

    bot: Bot = app.state.staff_bot
    due_line = f"\nСрок: {tc.due_at}" if tc.due_at else ""
    msg = f"Поручение\n{tc.title}\n{tc.body or ''}{due_line}\n\nОтветьте «готово» или /done когда выполните."
    try:
        await bot.send_message(int(assignee), msg)
        for att in saved:
            fp = db.attachment_fs_path(att["stored_path"])
            buf = BufferedInputFile(fp.read_bytes(), filename=att["file_name"])
            await bot.send_document(int(assignee), buf)
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
    with db.get_conn() as con:
        assignee = _resolve_assignee(con, body)
        task_id = db.create_task_and_send(con, assignee, body.title, body.body, body.due_at)

    bot: Bot = app.state.staff_bot
    due_line = f"\nСрок: {body.due_at}" if body.due_at else ""
    msg = f"Поручение\n{body.title}\n{body.body or ''}{due_line}\n\nОтветьте «готово» или /done когда выполните."
    try:
        await bot.send_message(int(assignee), msg)
    except Exception as e:
        logger.exception("deliver task to staff")
        raise HTTPException(status_code=502, detail=f"telegram_send_failed: {e!s}") from e

    with db.get_conn() as con:
        db.set_task_sent(con, task_id)

    return {"task_id": task_id, "assignee_tg_id": assignee, "status": "sent"}


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
