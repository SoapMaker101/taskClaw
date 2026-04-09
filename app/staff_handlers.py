from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app import db
from app.notify import notify_chairman

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

router = Router(name="staff")

waiting_fio: set[int] = set()

_DONE_RE = re.compile(r"^(готово|выполнил|сдал|done)\b", re.IGNORECASE)


def _fmt_done(task: dict, full_name: str, note: str) -> str:
    return (
        f"Задача выполнена\n"
        f"ID: {task['id']}\n"
        f"Исполнитель: {full_name} (tg {task['assignee_tg_id']})\n"
        f"Тема: {task['title']}\n"
        f"Комментарий: {note}"
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not message.from_user:
        return
    waiting_fio.add(message.from_user.id)
    await message.answer("Напишите ФИО одной строкой для регистрации.")


@router.message(Command("done"))
async def cmd_done(message: Message) -> None:
    await _try_complete(message, note="")


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

    await message.answer("Зафиксировал выполнение. Спасибо.")
    try:
        await notify_chairman(_fmt_done(task, full_name, note or "(без комментария)"))
    except Exception:
        logger.exception("notify chairman after done")
