from __future__ import annotations

import logging
import re
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, PhotoSize

from app import db
from app.notify import notify_chairman, forward_attachments_to_chairman

logger = logging.getLogger(__name__)

router = Router(name="staff")

waiting_fio: set[int] = set()

_DONE_RE = re.compile(r"^(готово|выполнил|сдал|done)\b", re.IGNORECASE)


def _fmt_done(task: dict, full_name: str, note: str, attachment_count: int = 0) -> str:
    att_line = f"\nФайлов приложено: {attachment_count}" if attachment_count else ""
    return (
        f"Задача выполнена\n"
        f"ID: {task['id']}\n"
        f"Исполнитель: {full_name} (tg {task['assignee_tg_id']})\n"
        f"Тема: {task['title']}\n"
        f"Комментарий: {note}{att_line}"
    )


async def _download_tg_file(bot: Bot, file_id: str) -> tuple[bytes, str]:
    tg_file = await bot.get_file(file_id)
    assert tg_file.file_path
    from io import BytesIO
    buf = BytesIO()
    await bot.download_file(tg_file.file_path, buf)
    return buf.getvalue(), tg_file.file_path.split("/")[-1]


def _find_open_task(uid: str) -> Optional[dict]:
    with db.get_conn() as con:
        row = con.execute(
            "SELECT * FROM tasks WHERE assignee_tg_id = ? AND status = 'sent' "
            "ORDER BY datetime(created_at) DESC LIMIT 1",
            (uid,),
        ).fetchone()
    return dict(row) if row else None


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not message.from_user:
        return
    waiting_fio.add(message.from_user.id)
    await message.answer("Напишите ФИО одной строкой для регистрации.")


@router.message(Command("done"))
async def cmd_done(message: Message) -> None:
    await _try_complete(message, note="")


@router.message(F.document)
async def on_document(message: Message, bot: Bot) -> None:
    """Staff sends a document — attach to their open task."""
    if not message.from_user:
        return
    uid = str(message.from_user.id)
    task = _find_open_task(uid)
    if not task:
        await message.answer("Нет открытой задачи, к которой можно приложить файл.")
        return

    doc = message.document
    assert doc
    data, _ = await _download_tg_file(bot, doc.file_id)
    file_name = doc.file_name or "document"
    with db.get_conn() as con:
        db.save_attachment(
            con, task["id"], file_name, data,
            mime_type=doc.mime_type, uploaded_by=uid, phase="completion",
        )
    await message.answer(f"Файл «{file_name}» приложен к задаче «{task['title']}».")

    caption = message.caption or ""
    if caption and _DONE_RE.search(caption.strip()):
        await _try_complete(message, note=caption.strip())


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    """Staff sends a photo — attach the highest-resolution version."""
    if not message.from_user:
        return
    uid = str(message.from_user.id)
    task = _find_open_task(uid)
    if not task:
        await message.answer("Нет открытой задачи, к которой можно приложить фото.")
        return

    assert message.photo
    best: PhotoSize = message.photo[-1]
    data, orig_name = await _download_tg_file(bot, best.file_id)
    file_name = orig_name if "." in orig_name else f"{orig_name}.jpg"
    with db.get_conn() as con:
        db.save_attachment(
            con, task["id"], file_name, data,
            mime_type="image/jpeg", uploaded_by=uid, phase="completion",
        )
    await message.answer(f"Фото приложено к задаче «{task['title']}».")

    caption = message.caption or ""
    if caption and _DONE_RE.search(caption.strip()):
        await _try_complete(message, note=caption.strip())


@router.message(F.text)
async def on_text(message: Message) -> None:
    if not message.from_user or not message.text:
        return
    uid = message.from_user.id
    text = message.text.strip()

    if uid in waiting_fio:
        waiting_fio.discard(uid)
        with db.get_conn() as con:
            db.upsert_contact(con, str(uid), text, message.from_user.username)
            con.commit()
        await message.answer(f"Записал: {text}. Жду поручения.")
        return

    if text.lower() == "/done" or _DONE_RE.search(text):
        note = text if not text.lower().startswith("/done") else ""
        await _try_complete(message, note=note)
        return


async def _try_complete(message: Message, note: str) -> None:
    assert message.from_user
    uid = str(message.from_user.id)
    with db.get_conn() as con:
        row = con.execute("SELECT full_name FROM contacts WHERE tg_user_id = ?", (uid,)).fetchone()
        if not row:
            await message.answer("Сначала /start и ФИО.")
            return
        full_name = row["full_name"]
        task = db.complete_latest_open_task(con, uid, note or "(без комментария)")
        if not task:
            await message.answer("Нет открытой задачи в статусе «отправлено».")
            return
        attachments = db.list_attachments(con, task["id"])
        completion_files = [a for a in attachments if a["phase"] == "completion"]

    await message.answer("Зафиксировал выполнение. Спасибо.")
    try:
        await notify_chairman(
            _fmt_done(task, full_name, note or "(без комментария)", len(completion_files))
        )
        if completion_files:
            await forward_attachments_to_chairman(completion_files)
    except Exception:
        logger.exception("notify chairman after done")
