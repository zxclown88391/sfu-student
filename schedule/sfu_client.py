import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass
import requests
import urllib3

from cache.store import cache_path, read_cache, write_cache
from database.db import SFU_TZ

log = logging.getLogger("sfu_student.schedule")

API_BASE = "https://edu.sfu-kras.ru/api/timetable"
USER_AGENT = "Mozilla/5.0"
API_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://mindmatters.ru/timetable",
    "Origin": "https://mindmatters.ru",
}
CACHE_TTL_MINUTES = 20

DAYS = {1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб", 7: "Вс"}
DAYS_FULL = {
    1: "Понедельник",
    2: "Вторник",
    3: "Среда",
    4: "Четверг",
    5: "Пятница",
    6: "Суббота",
    7: "Воскресенье",
}
MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _ssl_verify():
    val = os.getenv("SSL_VERIFY", "true").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return False
    return val


def api_get(path: str, params=None):
    verify = _ssl_verify()
    response = requests.get(
        f"{API_BASE}/{path}",
        params=params,
        headers=API_HEADERS,
        timeout=(8, 25),
        verify=verify,
    )
    if not response.ok:
        log.error("API СФУ: HTTP %s для %s; ответ: %s", response.status_code, response.url, response.text[:500])
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        log.error("API СФУ вернул не JSON для %s: %s", response.url, response.text[:500])
        raise ValueError("СФУ вернул ответ не в формате JSON") from exc


async def get_groups(force: bool = False):
    path = cache_path("institutes_groups")
    saved_at, cached = read_cache(path)
    if cached and not force:
        return cached
    try:
        data = await asyncio.to_thread(api_get, "get_insts")
        if isinstance(data, dict):
            for key in ("data", "result", "items", "groups", "institutes"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list) or not data:
            raise ValueError("API СФУ вернул пустой или неожиданный список групп")
        write_cache(path, data)
        return data
    except Exception:
        if cached:
            return cached
        raise


def structure_groups(items):
    result = {}
    for item in items:
        institute = str(item.get("institute") or "Другие группы").strip()
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        m = re.match(r"^(.*?)\s*\((.*?)\)$", name)
        group, subgroup = (m.group(1).strip(), m.group(2).strip()) if m else (name, "1 подгруппа")
        # Убираем служебный период действия вида «с 01.09 по 16.01.27»:
        # он ломает и показ в интерфейсе, и запрос расписания.
        group = clean_target(group)
        if not group:
            continue
        result.setdefault(institute, {}).setdefault(group, [])
        if subgroup not in result[institute][group]:
            result[institute][group].append(subgroup)
    return result


def clean_target(name: str) -> str:
    return re.sub(r"\s+с\s+\d{2}\.\d{2}(?:\.\d{2})?\s+по\s+\d{2}\.\d{2}(?:\.\d{2})?", "", name).strip()


def _extract_timetable(raw):
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("timetable", "lessons", "data", "result", "items", "schedule"):
            val = raw.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                for subkey in ("timetable", "lessons", "items", "data"):
                    if isinstance(val.get(subkey), list):
                        return val[subkey]
    return None


async def get_lessons(api_name: str, force: bool = False, fallback_targets=None):
    path = cache_path("schedule_" + api_name)
    saved_at, cached = read_cache(path)
    if cached and not force and saved_at:
        try:
            age = datetime.now(SFU_TZ) - datetime.fromisoformat(saved_at)
            if age < timedelta(minutes=CACHE_TTL_MINUTES):
                return cached, False, age
        except Exception:
            pass

    candidates = [api_name]
    if fallback_targets:
        for t in fallback_targets:
            if t and t not in candidates:
                candidates.append(t)

    try:
        timetable = None
        for cand in candidates:
            raw = await asyncio.to_thread(api_get, "get", {"target": cand})
            extracted = _extract_timetable(raw)
            if extracted is not None:
                timetable = extracted
                if extracted:
                    break
        if timetable is None:
            raise ValueError("Неожиданный формат расписания")
        write_cache(path, timetable)
        return timetable, False, timedelta(0)
    except Exception as exc:
        log.warning("Ошибка API расписания для %r: %s", api_name, exc)
        if cached is not None:
            age = datetime.now(SFU_TZ) - datetime.fromisoformat(saved_at) if saved_at else None
            return cached, True, age
        raise


def academic_week(date_obj: datetime) -> str:
    start = datetime(date_obj.year, 9, 1, tzinfo=SFU_TZ)
    if date_obj < start:
        start = datetime(date_obj.year - 1, 9, 1, tzinfo=SFU_TZ)
    return "1" if ((date_obj - start).days // 7 + 1) % 2 else "2"


def date_info(offset: int = 0, week_override=None):
    d = datetime.now(SFU_TZ) + timedelta(days=offset)
    w = str(week_override) if week_override in ("1", "2") else academic_week(d)
    return d.isoweekday(), w, f"{d.day} {MONTHS[d.month - 1]}"


def esc(value) -> str:
    return html.escape(str(value or "").strip())


def parse_time(value):
    m = re.search(r"(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})", str(value))
    return (m.group(1), m.group(2)) if m else ("", "")


def matching_week(item, week: str) -> bool:
    value = str(item.get("week", ""))
    return not value or value == "0" or str(week) in value


def format_lesson(item, week_label=None) -> str:
    subject = esc(item.get("subject") or "Занятие")
    time_str = esc(item.get("time"))
    teacher = esc(item.get("teacher"))
    room = esc(item.get("room"))
    building = esc(item.get("building"))
    kind = esc(item.get("type"))
    lines = [f"⏰ <code>{time_str}</code> — <b>{subject}</b>"]
    if week_label:
        lines[0] += f" <i>({esc(week_label)})</i>"
    location = []
    if building:
        location.append(building if "корпус" in building.lower() else f"Корпус {building}")
    if room:
        location.append(room if "ауд" in room.lower() else f"ауд. {room}")
    if location:
        lines.append("📍 " + ", ".join(location))
    if teacher:
        lines.append("👤 " + teacher)
    if kind:
        lines.append("📝 " + kind)
    sync = str(item.get("sync") or "").lower()
    if "асинхрон" in sync:
        lines.append("🌐 Асинхронно")
    return "\n".join(lines)


def format_day(raw, day: int, week: str, title: str) -> str:
    lessons = [x for x in raw if str(x.get("day")) == str(day) and matching_week(x, week)]
    lessons.sort(key=lambda x: parse_time(x.get("time"))[0] or "99:99")
    if not lessons:
        return f"<b>{title}</b>\n\nПар по данным расписания нет. Можно выдохнуть ☕"
    return f"<b>{title}</b>\n\n" + "\n\n".join(format_lesson(x) for x in lessons)


def format_week(raw, week: str, title: str) -> str:
    groups = {}
    for item in raw:
        if matching_week(item, week):
            groups.setdefault(str(item.get("day", "0")), []).append(item)
    parts = [f"<b>{title}</b>"]
    for day in sorted(groups, key=lambda x: int(x) if x.isdigit() else 99):
        if day not in [str(i) for i in range(1, 8)]:
            continue
        parts.append(f"\n<b>{DAYS_FULL[int(day)]}</b>")
        groups[day].sort(key=lambda x: parse_time(x.get("time"))[0] or "99:99")
        parts.extend(format_lesson(x) for x in groups[day])
    return "\n\n".join(parts) if len(parts) > 1 else f"<b>{title}</b>\n\nНа этой неделе пар нет."


TEACHER_CACHE_TTL_MINUTES = int(os.getenv("TEACHER_CACHE_TTL", "360"))


def _teacher_cache_path(institute: str):
    safe = re.sub(r"[^\w\-]+", "_", institute, flags=re.UNICODE)[:80] or "all"
    return cache_path(f"teachers_{safe}")


async def build_teacher_index(catalog: dict, institute: str, force: bool = False):
    """Собирает индекс преподавателей института: ФИО -> предметы, группы, занятия.

    Сканирование всех групп института — дорогая операция (по одному запросу на группу),
    поэтому результат кладётся в файловый кэш и переиспользуется.
    """
    path = _teacher_cache_path(institute)
    saved_at, cached = read_cache(path)
    if cached and not force and saved_at:
        try:
            age = datetime.now(SFU_TZ) - datetime.fromisoformat(saved_at)
            if age < timedelta(minutes=TEACHER_CACHE_TTL_MINUTES):
                return cached
        except Exception:
            pass

    groups = list(catalog.get(institute, {}))
    sem = asyncio.Semaphore(6)

    async def one(group):
        async with sem:
            try:
                raw, _, _ = await get_lessons(group)
                return group, raw
            except Exception:
                return group, []

    results = await asyncio.gather(*(one(g) for g in groups))

    index: dict = {}
    for group, raw in results:
        for item in raw:
            teacher = str(item.get("teacher") or "").strip()
            if not teacher:
                continue
            entry = index.setdefault(
                teacher,
                {"teacher": teacher, "subjects": [], "groups": [], "lessons": []},
            )
            subject = str(item.get("subject") or "").strip()
            if subject and subject not in entry["subjects"]:
                entry["subjects"].append(subject)
            if group not in entry["groups"]:
                entry["groups"].append(group)
            entry["lessons"].append(
                {
                    "group": group,
                    "day": item.get("day"),
                    "time": item.get("time"),
                    "subject": subject,
                    "type": item.get("type"),
                    "room": item.get("room"),
                    "building": item.get("building"),
                    "week": item.get("week"),
                }
            )

    for entry in index.values():
        entry["subjects"].sort()
        entry["groups"].sort()
        entry["lessons"].sort(
            key=lambda lesson: (
                int(lesson["day"]) if str(lesson.get("day", "")).isdigit() else 99,
                parse_time(lesson.get("time"))[0] or "99:99",
            )
        )

    if index:
        write_cache(path, index)
    return index


def search_teachers(index: dict, query: str, limit: int = 20):
    """Нечёткий поиск по ФИО: все слова запроса должны найтись в строке."""
    words = [w for w in re.split(r"\s+", (query or "").strip().lower()) if w]
    if not words:
        return []
    scored = []
    for entry in index.values():
        name = entry["teacher"].lower()
        if not all(word in name for word in words):
            continue
        # Совпадение с начала фамилии — выше в выдаче.
        starts = 0 if name.startswith(words[0]) else 1
        scored.append((starts, len(entry["teacher"]), entry))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in scored[:limit]]


async def find_teacher(catalog: dict, institute: str, query: str):
    """Совместимость со старым вызовом: возвращает список {group, lesson}."""
    index = await build_teacher_index(catalog, institute)
    found, seen = [], set()
    for entry in search_teachers(index, query, limit=10):
        for lesson in entry["lessons"]:
            key = (lesson.get("group"), lesson.get("day"), lesson.get("time"), lesson.get("subject"))
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "group": lesson["group"],
                    "lesson": {
                        "day": lesson.get("day"),
                        "time": lesson.get("time"),
                        "subject": lesson.get("subject"),
                        "teacher": entry["teacher"],
                        "room": lesson.get("room"),
                        "building": lesson.get("building"),
                        "type": lesson.get("type"),
                        "week": lesson.get("week"),
                    },
                }
            )
    return found


def get_current_status(raw):
    day, week_num, _ = date_info()
    today = [x for x in raw if str(x.get("day")) == str(day) and matching_week(x, week_num)]
    today.sort(key=lambda x: parse_time(x.get("time"))[0] or "99:99")
    current = datetime.now(SFU_TZ).strftime("%H:%M")
    active = None
    next_lesson = None
    for item in today:
        start, end = parse_time(item.get("time"))
        if start and start <= current < end:
            active = item
            break
        if start and current < start:
            next_lesson = item
            break
    remaining = None
    if active:
        _, b = parse_time(active.get("time"))
        if b:
            remaining = max(0, int((datetime.strptime(b, "%H:%M") - datetime.strptime(current, "%H:%M")).total_seconds() // 60))
    return {"state": "active" if active else ("next" if next_lesson else "free"), "active": active, "next": next_lesson, "remaining": remaining}
