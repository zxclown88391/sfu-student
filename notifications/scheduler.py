import asyncio
import logging
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database.db import SFU_TZ, get_conn
from schedule.sfu_client import DAYS_FULL, date_info, esc, format_day, get_lessons

log = logging.getLogger("sfu_student.notifications")


async def notifications_job(bot, keyboard_builder):
    now_text = datetime.now(SFU_TZ).strftime("%H:%M")
    with get_conn() as con:
        users = [
            dict(x)
            for x in con.execute(
                "SELECT * FROM users WHERE notif_enabled=1 AND notif_time=?",
                (now_text,),
            ).fetchall()
        ]
    for user in users:
        try:
            offset = int(user["notif_target"])
            day, w, date_label = date_info(offset)
            raw, stale, _ = await get_lessons(user["api_name"])
            title = (
                f"📚 <b>Расписание на {'сегодня' if offset == 0 else 'завтра'}</b>\n"
                f"{esc(user['display_name'])} · {DAYS_FULL[day]}, {date_label}"
            )
            text = format_day(raw, day, w, title)
            if stale:
                text += "\n\n⚠️ Сайт СФУ недоступен. Это сохранённое расписание."
            await bot.send_message(
                user["user_id"],
                text,
                parse_mode="HTML",
                reply_markup=keyboard_builder(user["user_id"]),
            )
            await asyncio.sleep(0.08)
        except Exception:
            log.exception("Не удалось отправить уведомление пользователю %s", user["user_id"])


def create_scheduler(bot, keyboard_builder) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=str(SFU_TZ))
    scheduler.add_job(
        notifications_job,
        "cron",
        minute="*",
        args=[bot, keyboard_builder],
        id="daily_schedule_notifications",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=50,
    )
    return scheduler
