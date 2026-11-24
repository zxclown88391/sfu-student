import asyncio
import logging
import os
import re
from datetime import datetime

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from auth.tokens import sign_session
from database.db import (
    SFU_TZ,
    build_display_name,
    ensure_user,
    get_user,
    init_db,
    save_user,
    update_user,
)
from integrations.sfu_hub import get_eios_links
from notifications.scheduler import create_scheduler
from schedule.sfu_client import (
    DAYS_FULL,
    academic_week,
    build_teacher_index,
    clean_target,
    date_info,
    esc,
    format_day,
    format_lesson,
    format_week,
    get_current_status,
    get_groups,
    get_lessons,
    parse_time,
    search_teachers,
    structure_groups,
)

log = logging.getLogger("sfu_student.bot")

DIVIDER = "—"

# Версия mini app: меняйте при обновлении файла, чтобы Telegram не показывал
# закэшированную старую страницу.
MINIAPP_VERSION = "4"


class Setup(StatesGroup):
    institute = State()
    group = State()
    subgroup = State()
    teacher = State()


# --------------------------------------------------------------------------- #
# Ссылка на mini app
# --------------------------------------------------------------------------- #


def public_api_base() -> str:
    """Публичный адрес API: отдаёт и /miniapp, и /api/*."""
    explicit = (os.getenv("PUBLIC_API_URL", "") or "").strip().rstrip("/")
    if explicit:
        return explicit
    url = (os.getenv("MINIAPP_URL", "") or os.getenv("RENDER_EXTERNAL_URL", "")).strip().rstrip("/")
    if url.endswith("/miniapp"):
        url = url[: -len("/miniapp")]
    return url


def app_page_url() -> str:
    """Страница mini app. Может жить отдельно от API — например, на GitHub Pages."""
    page = (os.getenv("MINIAPP_URL", "") or "").strip()
    if page:
        return page
    api = public_api_base()
    return f"{api}/miniapp" if api else ""


def miniapp_url(user_id: int) -> str:
    """Ссылка с подписанным токеном: работает и в Telegram, и в обычном браузере."""
    page = app_page_url()
    if not page:
        return ""
    separator = "&" if "?" in page else "?"
    query = [f"t={sign_session(user_id)}"]
    api = public_api_base()
    if api:
        query.append(f"api={api}")
    query.append(f"v={MINIAPP_VERSION}")
    return f"{page}{separator}{'&'.join(query)}"


def menu_button_url() -> str:
    """Адрес mini app для кнопки меню чата (без токена: внутри Telegram придёт initData)."""
    page = app_page_url()
    if not page:
        return ""
    api = public_api_base()
    base = f"{page}?api={api}" if api else page
    return f"{base}&v={MINIAPP_VERSION}" if "?" in base else f"{base}?v={MINIAPP_VERSION}"


async def sync_menu_button(bot: Bot, user_id: int) -> None:
    """Кнопка меню задаётся в BotFather, и API её не переопределяет глобально.

    Поэтому для каждого, кто открыл бота, ставим персональную кнопку с актуальным
    адресом mini app. Ошибку не пробрасываем: это украшение, а не работа бота.
    """
    url = menu_button_url()
    if not url or not url.startswith("https://"):
        return
    try:
        await bot.set_chat_menu_button(
            chat_id=user_id,
            menu_button=types.MenuButtonWebApp(
                type="web_app",
                text="Расписание",
                web_app=WebAppInfo(url=url),
            ),
        )
    except Exception:
        log.warning("Не удалось обновить кнопку меню для %s", user_id, exc_info=False)


def app_button_row(user_id: int):
    url = miniapp_url(user_id)
    if not url:
        return []
    if url.startswith("https://"):
        return [[InlineKeyboardButton(text="Открыть приложение", web_app=WebAppInfo(url=url))]]
    return [[InlineKeyboardButton(text="Открыть приложение (локально)", url=url)]]


# --------------------------------------------------------------------------- #
# Клавиатуры
# --------------------------------------------------------------------------- #


def main_keyboard(user_id: int):
    user = get_user(user_id)
    rows = []
    if not user or not (user.get("api_name") or "").strip():
        rows.append([InlineKeyboardButton(text="Выбрать группу", callback_data="change_group")])
    else:
        rows.extend([
            [
                InlineKeyboardButton(text="Сегодня", callback_data="day:0"),
                InlineKeyboardButton(text="Завтра", callback_data="day:1"),
            ],
            [
                InlineKeyboardButton(text="Сейчас", callback_data="now"),
                InlineKeyboardButton(text="Неделя", callback_data="week"),
            ],
            [InlineKeyboardButton(text="Преподаватели", callback_data="teacher")],
        ])
    rows.extend(app_button_row(user_id))
    if user and (user.get("api_name") or "").strip():
        rows.append([
            InlineKeyboardButton(text="Сервисы", callback_data="eios"),
            InlineKeyboardButton(text="Настройки", callback_data="settings"),
        ])
        rows.append([InlineKeyboardButton(text="Сменить группу", callback_data="change_group")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def paged_keyboard(items, page: int, prefix: str, is_inst: bool = False):
    size = 10 if is_inst else 16
    pages = max(1, (len(items) + size - 1) // size)
    page = max(0, min(page, pages - 1))
    rows, row = [], []
    for idx, item in enumerate(items[page * size:(page + 1) * size], start=page * size):
        label = item[:30] if len(item) > 30 else item
        row.append(InlineKeyboardButton(text=label, callback_data=f"{prefix}:{idx}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="Назад", callback_data=f"{prefix}_page:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Далее", callback_data=f"{prefix}_page:{page + 1}"))
    rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------- #
# Отправка текста
# --------------------------------------------------------------------------- #


async def safe_answer(message, text: str, markup=None):
    chunks, current = [], ""
    for paragraph in text.split("\n\n"):
        piece = paragraph + "\n\n"
        if len(current) + len(piece) > 3800 and current:
            chunks.append(current)
            current = ""
        current += piece
    if current:
        chunks.append(current)
    for index, chunk in enumerate(chunks):
        await message.answer(
            chunk.strip(),
            parse_mode="HTML",
            reply_markup=markup if index == len(chunks) - 1 else None,
            disable_web_page_preview=True,
        )


def profile_line(user) -> str:
    if user and (user.get("api_name") or "").strip():
        return f"<b>{esc(user['display_name'])}</b> · {esc(user['institute'])}"
    return "Группа не выбрана"


async def send_schedule(message, user, mode: str, offset: int = 0, week=None):
    fallbacks = [user["display_name"], clean_target(user["display_name"]), clean_target(user["api_name"])]
    try:
        raw, stale, age = await get_lessons(user["api_name"], fallback_targets=fallbacks)
    except Exception:
        log.exception("Расписание недоступно")
        await message.answer(
            "Не удалось получить расписание СФУ. Попробуй через пару минут.",
            reply_markup=main_keyboard(user["user_id"]),
        )
        return

    warning = ""
    if stale:
        minutes = int(age.total_seconds() // 60) if age else 0
        warning = f"\n\nСФУ сейчас недоступен, показаны сохранённые данные ({minutes} мин. назад)."

    if mode == "day":
        day, w, date_label = date_info(offset, week_override=week)
        title = (
            f"{'Сегодня' if offset == 0 else 'Завтра'} — {date_label}\n"
            f"{esc(user['display_name'])} · {DAYS_FULL[day]}"
        )
        result = format_day(raw, day, w, title)
    else:
        w = str(week) if week in ("1", "2") else academic_week(datetime.now(SFU_TZ))
        title = f"{'Нечётная' if w == '1' else 'Чётная'} неделя\n{esc(user['display_name'])}"
        result = format_week(raw, w, title)
    await safe_answer(message, result + warning, main_keyboard(user["user_id"]))


# --------------------------------------------------------------------------- #
# Запуск
# --------------------------------------------------------------------------- #


async def run_bot():
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise SystemExit("Не найден BOT_TOKEN. Скопируйте .env.example в .env и укажите токен от BotFather.")

    init_db()
    bot = Bot(bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    catalog_cache: dict = {}

    async def load_catalog(force: bool = False):
        nonlocal catalog_cache
        if catalog_cache and not force:
            return catalog_cache
        items = await get_groups(force=force)
        catalog_cache.clear()
        catalog_cache.update(structure_groups(items))
        return catalog_cache

    async def begin_setup(message, user_id: int, state: FSMContext):
        await state.clear()
        try:
            catalog = await load_catalog()
        except Exception:
            log.exception("Не удалось загрузить каталог")
            await message.answer("Сейчас не удаётся загрузить список групп СФУ. Попробуй позже.")
            return
        institutes = sorted(catalog)
        await state.update_data(institutes=institutes)
        await message.answer(
            "Шаг 1 из 3. Выбери институт:",
            reply_markup=paged_keyboard(institutes, 0, "inst", True),
        )
        await state.set_state(Setup.institute)

    @dp.callback_query(F.data == "noop")
    async def noop(callback):
        await callback.answer()

    @dp.message(CommandStart())
    async def start(message: types.Message, state: FSMContext):
        user_id = message.from_user.id
        ensure_user(user_id, message.from_user.first_name or "", message.from_user.username or "")
        await sync_menu_button(message.bot, user_id)
        user = get_user(user_id)
        if user and (user.get("api_name") or "").strip():
            await message.answer(
                f"SFU Student\n{profile_line(user)}\n\nЧто смотрим?",
                parse_mode="HTML",
                reply_markup=main_keyboard(user_id),
            )
            return
        await message.answer("SFU Student — расписание, преподаватели и сервисы СФУ.")
        await begin_setup(message, user_id, state)

    @dp.message(Command("group"))
    async def cmd_group(message: types.Message, state: FSMContext):
        ensure_user(message.from_user.id, message.from_user.first_name or "", message.from_user.username or "")
        await begin_setup(message, message.from_user.id, state)

    @dp.message(Command("today"))
    async def cmd_today(message: types.Message):
        await schedule_command(message, "day", 0)

    @dp.message(Command("tomorrow"))
    async def cmd_tomorrow(message: types.Message):
        await schedule_command(message, "day", 1)

    @dp.message(Command("week"))
    async def cmd_week(message: types.Message):
        await schedule_command(message, "week", 0)

    async def schedule_command(message, mode: str, offset: int):
        user = get_user(message.from_user.id)
        if not user or not (user.get("api_name") or "").strip():
            await message.answer(
                "Сначала выбери группу — команда /group.",
                reply_markup=main_keyboard(message.from_user.id),
            )
            return
        await send_schedule(message, user, mode, offset)

    @dp.message(Command("teacher"))
    async def cmd_teacher(message: types.Message, command: CommandObject, state: FSMContext):
        query = (command.args or "").strip()
        if not query:
            await message.answer("Напиши фамилию: /teacher Иванов")
            return
        await run_teacher_search(message, query, state)

    # ---------------- выбор группы ----------------

    @dp.callback_query(Setup.institute, F.data.startswith("inst_page:"))
    async def inst_page(callback, state):
        data = await state.get_data()
        page = int(callback.data.split(":")[1])
        await callback.answer()
        await callback.message.edit_reply_markup(
            reply_markup=paged_keyboard(data["institutes"], page, "inst", True)
        )

    @dp.callback_query(Setup.institute, F.data.startswith("inst:"))
    async def inst_select(callback, state):
        await callback.answer()
        data = await state.get_data()
        institute = data["institutes"][int(callback.data.split(":")[1])]
        catalog = await load_catalog()
        groups = sorted(catalog[institute])
        await state.update_data(institute=institute, groups=groups)
        await callback.message.edit_text(
            f"Шаг 2 из 3. {esc(institute)}\nВыбери группу:",
            parse_mode="HTML",
            reply_markup=paged_keyboard(groups, 0, "grp"),
        )
        await state.set_state(Setup.group)

    @dp.callback_query(Setup.group, F.data.startswith("grp_page:"))
    async def grp_page(callback, state):
        data = await state.get_data()
        await callback.answer()
        await callback.message.edit_reply_markup(
            reply_markup=paged_keyboard(data["groups"], int(callback.data.split(":")[1]), "grp")
        )

    @dp.callback_query(Setup.group, F.data.startswith("grp:"))
    async def grp_select(callback, state):
        await callback.answer()
        data = await state.get_data()
        group = data["groups"][int(callback.data.split(":")[1])]
        catalog = await load_catalog()
        subgroups = catalog[data["institute"]][group]

        if len(subgroups) <= 1:
            await finish_setup(callback, state, group, subgroups[0] if subgroups else "")
            return

        await state.update_data(group=group, subgroups=subgroups)
        buttons = [[InlineKeyboardButton(text=s, callback_data=f"sub:{i}")] for i, s in enumerate(subgroups)]
        buttons.append([InlineKeyboardButton(text="Отмена", callback_data="setup_cancel")])
        await callback.message.edit_text(
            f"Шаг 3 из 3. {esc(group)}\nВыбери подгруппу:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await state.set_state(Setup.subgroup)

    @dp.callback_query(Setup.subgroup, F.data.startswith("sub:"))
    async def subgroup_select(callback, state):
        await callback.answer()
        data = await state.get_data()
        subgroup = data["subgroups"][int(callback.data.split(":")[1])]
        await finish_setup(callback, state, data["group"], subgroup)

    async def finish_setup(callback, state, group: str, subgroup: str):
        data = await state.get_data()
        institute = data.get("institute", "")
        display = build_display_name(group, subgroup)
        save_user(
            callback.from_user.id,
            display,
            group,
            institute,
            subgroup,
            first_name=callback.from_user.first_name or "",
            username=callback.from_user.username or "",
        )
        await state.clear()
        user = get_user(callback.from_user.id)
        await callback.message.edit_text(
            f"Готово. Твоя группа: <b>{esc(display)}</b>\n{esc(institute)}",
            parse_mode="HTML",
        )
        await callback.message.answer(
            "Теперь доступны расписание, преподаватели и уведомления.\n"
            "Выбор сохранён и в боте, и в приложении.",
            reply_markup=main_keyboard(callback.from_user.id),
        )

    @dp.callback_query(F.data == "setup_cancel")
    async def setup_cancel(callback, state):
        await callback.answer()
        await state.clear()
        await callback.message.edit_text("Настройка отменена. Команда /start вернёт меню.")

    @dp.callback_query(F.data == "change_group")
    async def change_group(callback, state):
        await callback.answer()
        await begin_setup(callback.message, callback.from_user.id, state)

    # ---------------- расписание ----------------

    @dp.callback_query(F.data.startswith("day:"))
    async def day_cb(callback):
        await callback.answer()
        user = get_user(callback.from_user.id)
        if user and (user.get("api_name") or "").strip():
            await send_schedule(callback.message, user, "day", int(callback.data.split(":")[1]))
        else:
            await callback.message.answer("Сначала выбери группу: /group")

    @dp.callback_query(F.data == "week")
    async def week_cb(callback):
        await callback.answer()
        user = get_user(callback.from_user.id)
        if user and (user.get("api_name") or "").strip():
            await send_schedule(callback.message, user, "week")
        else:
            await callback.message.answer("Сначала выбери группу: /group")

    @dp.callback_query(F.data == "now")
    async def now_cb(callback):
        await callback.answer()
        user = get_user(callback.from_user.id)
        if not user or not (user.get("api_name") or "").strip():
            await callback.message.answer("Сначала выбери группу: /group")
            return
        try:
            raw, stale, _ = await get_lessons(user["api_name"])
        except Exception:
            await callback.message.answer("Не удалось обновить расписание. Попробуй позже.")
            return
        status = get_current_status(raw)
        if status["state"] == "active":
            answer = (
                f"Сейчас идёт занятие\n\n{format_lesson(status['active'])}\n\n"
                f"До конца примерно {status['remaining']} мин."
            )
        elif status["state"] == "next":
            answer = f"Сейчас свободное время\n\nСледующая пара:\n{format_lesson(status['next'])}"
        else:
            answer = "Сейчас занятий нет — на сегодня всё."
        if stale:
            answer += "\n\nСФУ недоступен, данные из кэша."
        await callback.message.answer(answer, parse_mode="HTML", reply_markup=main_keyboard(callback.from_user.id))

    # ---------------- преподаватели ----------------

    @dp.callback_query(F.data == "teacher")
    async def teacher_start(callback, state):
        await callback.answer()
        user = get_user(callback.from_user.id)
        if not user or not (user.get("api_name") or "").strip():
            await callback.message.answer("Сначала выбери группу: /group")
            return
        await callback.message.answer(
            "Напиши фамилию преподавателя — например, «Иванов».\n"
            "Поиск идёт по расписанию всех групп твоего института."
        )
        await state.set_state(Setup.teacher)

    @dp.message(Setup.teacher)
    async def teacher_search(message: types.Message, state: FSMContext):
        query = (message.text or "").strip()
        # Команды не считаем поисковым запросом: /group и /start должны работать всегда.
        if query.startswith("/"):
            await state.clear()
            return
        await state.clear()
        if query:
            await run_teacher_search(message, query, state)

    async def run_teacher_search(message, query: str, state: FSMContext):
        user = get_user(message.from_user.id)
        if not user or not (user.get("institute") or "").strip():
            await message.answer("Сначала выбери группу: /group")
            return
        note = await message.answer("Ищу преподавателя в расписании института…")
        try:
            catalog = await load_catalog()
            institute = user["institute"]
            if institute not in catalog:
                await note.edit_text("Институт не найден в каталоге. Смени группу: /group")
                return
            index = await build_teacher_index(catalog, institute)
            found = search_teachers(index, query, limit=8)
        except Exception:
            log.exception("Ошибка поиска преподавателя")
            await note.edit_text("Не удалось выполнить поиск. Попробуй позже.")
            return

        if not found:
            await note.edit_text(
                f"По запросу «{esc(query)}» никого не нашлось.\n"
                "Попробуй только фамилию или её часть.",
                parse_mode="HTML",
                reply_markup=main_keyboard(message.from_user.id),
            )
            return

        lines = [f"Преподаватели по запросу «{esc(query)}»"]
        for entry in found[:8]:
            subjects = entry["subjects"][:3]
            groups = entry["groups"][:4]
            lines.append(
                f"<b>{esc(entry['teacher'])}</b>\n"
                + (f"{esc(', '.join(subjects))}\n" if subjects else "")
                + (f"Группы: {esc(', '.join(groups))}" if groups else "")
            )
        await note.delete()
        await safe_answer(message, "\n\n".join(lines), main_keyboard(message.from_user.id))

    # ---------------- сервисы и настройки ----------------

    @dp.callback_query(F.data == "eios")
    async def eios_cb(callback):
        await callback.answer()
        links = get_eios_links()
        rows = [[InlineKeyboardButton(text=item["title"], url=item["url"])] for item in links]
        rows.append([InlineKeyboardButton(text="В меню", callback_data="back")])
        await callback.message.answer(
            "Сервисы СФУ",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @dp.callback_query(F.data == "settings")
    async def settings_cb(callback):
        await callback.answer()
        user = get_user(callback.from_user.id)
        if not user:
            await callback.message.answer("Сначала выбери группу: /group")
            return
        enabled = bool(user["notif_enabled"])
        target = "завтра" if user["notif_target"] else "сегодня"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"Рассылка: {'вкл' if enabled else 'выкл'}",
                    callback_data="notif_toggle",
                )],
                [InlineKeyboardButton(text=f"Время: {user['notif_time']}", callback_data="notif_time")],
                [InlineKeyboardButton(text=f"Присылать: {target}", callback_data="notif_target")],
                [InlineKeyboardButton(text="В меню", callback_data="back")],
            ]
        )
        await callback.message.answer(
            f"Настройки\n{profile_line(user)}",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    @dp.callback_query(F.data == "notif_toggle")
    async def notif_toggle(callback):
        await callback.answer()
        user = get_user(callback.from_user.id)
        if user:
            update_user(callback.from_user.id, notif_enabled=0 if user["notif_enabled"] else 1)
            await callback.message.answer("Готово.", reply_markup=main_keyboard(callback.from_user.id))

    @dp.callback_query(F.data == "notif_time")
    async def notif_time(callback):
        await callback.answer()
        times = ["07:00", "08:00", "09:00", "12:00", "18:00", "20:00"]
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=t, callback_data=f"time:{t}") for t in times[i:i + 3]]
                for i in range(0, len(times), 3)
            ]
        )
        await callback.message.answer("Выбери время рассылки:", reply_markup=keyboard)

    @dp.callback_query(F.data.startswith("time:"))
    async def set_time(callback):
        await callback.answer()
        value = callback.data.split(":", 1)[1]
        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            update_user(callback.from_user.id, notif_time=value)
            await callback.message.answer(
                f"Рассылка в {value}.",
                reply_markup=main_keyboard(callback.from_user.id),
            )

    @dp.callback_query(F.data == "notif_target")
    async def notif_target(callback):
        await callback.answer()
        user = get_user(callback.from_user.id)
        if user:
            update_user(callback.from_user.id, notif_target=0 if user["notif_target"] else 1)
            await callback.message.answer("Готово.", reply_markup=main_keyboard(callback.from_user.id))

    @dp.callback_query(F.data == "back")
    async def back(callback):
        await callback.answer()
        await callback.message.answer("Меню", reply_markup=main_keyboard(callback.from_user.id))

    scheduler = create_scheduler(bot, main_keyboard)
    scheduler.start()
    log.info("SFU Student запущен")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
