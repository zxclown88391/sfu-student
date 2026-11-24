import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from auth.tokens import SessionError, sign_session, verify_session
from database.db import (
    SFU_TZ,
    add_favorite,
    build_display_name,
    ensure_user,
    get_favorites,
    get_user,
    get_user_public,
    init_db,
    remove_favorite,
    save_user,
    update_user,
)
from integrations.sfu_hub import get_announcements, get_campus_news, get_eios_links
from schedule.sfu_client import (
    DAYS,
    DAYS_FULL,
    academic_week,
    build_teacher_index,
    clean_target,
    date_info,
    get_current_status,
    get_groups,
    get_lessons,
    matching_week,
    parse_time,
    search_teachers,
    structure_groups,
)

log = logging.getLogger("sfu_student.api")

init_db()
app = FastAPI(title="SFU Student API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

MINIAPP_DIR = Path(__file__).resolve().parent.parent / "miniapp"

# Кэш структуры институтов в памяти процесса: файл читается один раз.
_CATALOG: dict = {"items": None, "structured": None}


# --------------------------------------------------------------------------- #
# Авторизация
# --------------------------------------------------------------------------- #


def _telegram_user_id(request: Request) -> int:
    """Проверка подписи Telegram initData (алгоритм из документации WebApp)."""
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise HTTPException(status_code=503, detail="BOT_TOKEN не настроен на сервере")

    raw_init_data = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not raw_init_data:
        raise HTTPException(status_code=401, detail="Открой Mini App внутри Telegram")

    try:
        pairs = parse_qsl(raw_init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise HTTPException(status_code=401, detail="Некорректный initData")
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise HTTPException(status_code=401, detail="Некорректный initData")

    received_hash = fields.pop("hash", "")
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not received_hash or not hmac.compare_digest(received_hash, expected_hash):
        raise HTTPException(status_code=401, detail="Подпись Telegram не совпала")

    try:
        auth_date = int(fields["auth_date"])
        user = json.loads(fields["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Некорректные поля initData")

    now = int(time.time())
    if auth_date > now + 60 or now - auth_date > 86400:
        raise HTTPException(status_code=401, detail="Сессия Telegram истекла, открой приложение заново")

    ensure_user(
        user_id,
        first_name=str(user.get("first_name") or ""),
        username=str(user.get("username") or ""),
    )
    return user_id


def _session_user_id(request: Request) -> int:
    token = (
        request.headers.get("X-SFU-Token", "").strip()
        or (request.query_params.get("t") or "").strip()
    )
    auth_header = request.headers.get("Authorization", "").strip()
    if not token and auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Нужен токен сессии")
    try:
        user_id = verify_session(token)
    except SessionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    # Пользователь мог прийти по ссылке из бота раньше, чем бот создал запись.
    ensure_user(user_id)
    return user_id


def current_user_id(request: Request) -> int:
    """Единая авторизация: Telegram WebApp либо подписанный токен из бота.

    Мини-апп присылает оба заголовка, когда у него есть initData; в обычном браузере
    (GitHub Pages, локальный сервер) остаётся только токен из ссылки бота.
    """
    if request.headers.get("X-Telegram-Init-Data", "").strip():
        return _telegram_user_id(request)
    return _session_user_id(request)


def require_self(path_user_id: int, authenticated_user_id: int) -> None:
    if int(path_user_id) != int(authenticated_user_id):
        raise HTTPException(status_code=403, detail="Нельзя открыть чужой профиль")


# --------------------------------------------------------------------------- #
# Модели запросов
# --------------------------------------------------------------------------- #


class ProfilePayload(BaseModel):
    user_id: int | None = None
    api_name: str | None = None
    institute: str | None = None
    subgroup: str | None = None
    display_name: str | None = None
    notif_enabled: int | None = None
    notif_time: str | None = None
    notif_target: int | None = None
    theme: str | None = None
    lead_minutes: int | None = None


class FavoritePayload(BaseModel):
    user_id: int | None = None
    item_type: str
    title: str
    subtitle: str = ""


# --------------------------------------------------------------------------- #
# Служебные
# --------------------------------------------------------------------------- #


async def _catalog():
    if _CATALOG["items"] is None:
        _CATALOG["items"] = await get_groups()
        _CATALOG["structured"] = structure_groups(_CATALOG["items"])
    return _CATALOG["items"], _CATALOG["structured"]


async def _catalog_force(force: bool):
    if force or _CATALOG["items"] is None:
        _CATALOG["items"] = await get_groups(force=force)
        _CATALOG["structured"] = structure_groups(_CATALOG["items"])
    return _CATALOG["items"], _CATALOG["structured"]


def _lesson_sort_key(item):
    return parse_time(item.get("time"))[0] or "99:99"


def _schedule_payload(user, day_offset: int = 0, week: str | None = None):
    return {
        "user": get_user_public(user["user_id"]),
        "day_offset": day_offset,
        "week": week,
    }


# --------------------------------------------------------------------------- #
# Mini App: страница и здоровье
# --------------------------------------------------------------------------- #


@app.get("/miniapp")
async def miniapp_index():
    return FileResponse(MINIAPP_DIR / "index.html")


@app.get("/")
async def root():
    return FileResponse(MINIAPP_DIR / "index.html")


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "SFU Student API",
        "version": "3.0.0",
        "bot_configured": bool(os.getenv("BOT_TOKEN", "").strip()),
    }


@app.get("/api/config")
async def api_config():
    """Публичная конфигурация для mini app: где API и какой бот."""
    public_url = (os.getenv("MINIAPP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if public_url.endswith("/miniapp"):
        public_url = public_url[: -len("/miniapp")]
    return {
        "api_base": public_url,
        "app_name": "SFU Student",
        "version": "3.0.0",
    }


# --------------------------------------------------------------------------- #
# Каталог институтов и групп
# --------------------------------------------------------------------------- #


@app.get("/api/catalog")
async def api_catalog(force: bool = False, compact: bool = False):
    _, structured = await _catalog_force(force)
    if compact:
        return {
            "institutes": [
                {"name": inst, "groups": sorted(groups)}
                for inst, groups in sorted(structured.items())
            ]
        }
    return {
        "institutes": [
            {
                "name": inst,
                "groups": [
                    {"group": grp, "subgroups": subs}
                    for grp, subs in sorted(groups.items())
                ],
            }
            for inst, groups in sorted(structured.items())
        ]
    }


# --------------------------------------------------------------------------- #
# Профиль пользователя
# --------------------------------------------------------------------------- #


@app.get("/api/me")
async def api_me(user_id: int = Depends(current_user_id)):
    return {"user": get_user_public(user_id), "favorites": get_favorites(user_id)}


@app.post("/api/profile")
async def api_profile(payload: ProfilePayload, user_id: int = Depends(current_user_id)):
    if payload.user_id is not None:
        require_self(payload.user_id, user_id)

    ensure_user(user_id)
    updates: dict = {}

    if payload.api_name is not None:
        api_name = payload.api_name.strip()
        if not api_name:
            raise HTTPException(status_code=400, detail="Название группы не может быть пустым")
        subgroup = (payload.subgroup or "").strip()
        institute = (payload.institute or "").strip()
        existing = get_user(user_id) or {}
        updates["api_name"] = api_name
        updates["subgroup"] = subgroup
        updates["institute"] = institute or existing.get("institute", "")
        updates["display_name"] = build_display_name(api_name, subgroup)
    elif payload.display_name is not None:
        updates["display_name"] = payload.display_name.strip()

    if payload.notif_enabled is not None:
        updates["notif_enabled"] = 1 if payload.notif_enabled else 0
    if payload.notif_time is not None:
        value = payload.notif_time.strip()
        try:
            hour, minute = value.split(":")
            if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
                raise ValueError
            updates["notif_time"] = f"{int(hour):02d}:{int(minute):02d}"
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Время должно быть в формате ЧЧ:ММ") from exc
    if payload.notif_target is not None:
        updates["notif_target"] = 1 if payload.notif_target else 0
    if payload.theme in ("dark", "light"):
        updates["theme"] = payload.theme
    if payload.lead_minutes is not None:
        updates["lead_minutes"] = max(5, min(60, int(payload.lead_minutes)))

    if updates:
        update_user(user_id, **updates)
    return {"ok": True, "user": get_user_public(user_id)}


# --------------------------------------------------------------------------- #
# Расписание
# --------------------------------------------------------------------------- #


def _build_schedule(user, day_offset: int, week: str | None, raw, stale, age):
    now_dt = datetime.now(SFU_TZ)
    active_week = str(week) if week in ("1", "2") else academic_week(now_dt)
    day_num, _, date_label = date_info(day_offset, week_override=active_week)

    lessons = [
        x for x in raw
        if str(x.get("day")) == str(day_num) and matching_week(x, active_week)
    ]
    lessons.sort(key=_lesson_sort_key)

    week_days = []
    for d in range(1, 8):
        d_lessons = [
            x for x in raw
            if str(x.get("day")) == str(d) and matching_week(x, active_week)
        ]
        d_lessons.sort(key=_lesson_sort_key)
        week_days.append({
            "day": d,
            "short": DAYS[d],
            "full": DAYS_FULL[d],
            "lessons": d_lessons,
        })

    return {
        "user": get_user_public(user["user_id"]),
        "week": active_week,
        "day": day_num,
        "day_full": DAYS_FULL[day_num],
        "date_label": date_label,
        "stale": stale,
        "cache_age_minutes": int(age.total_seconds() // 60) if age else 0,
        "lessons": lessons,
        "week_days": week_days,
        "status": get_current_status(raw),
    }


async def _load_schedule(user, day_offset: int, week: str | None):
    fallbacks = [
        user["display_name"],
        clean_target(user["display_name"]),
        clean_target(user["api_name"]),
    ]
    raw, stale, age = await get_lessons(user["api_name"], fallback_targets=fallbacks)
    return _build_schedule(user, day_offset, week, raw, stale, age)


@app.get("/api/schedule")
async def api_schedule(
    day_offset: int = 0,
    week: str | None = None,
    user_id: int = Depends(current_user_id),
):
    user = get_user(user_id)
    if not user or not (user.get("api_name") or "").strip():
        raise HTTPException(status_code=404, detail="Группа не выбрана")
    return await _load_schedule(user, day_offset, week)


@app.get("/api/schedule/{path_user_id}")
async def api_schedule_legacy(
    path_user_id: int,
    day_offset: int = 0,
    week: str | None = None,
    view: str = "day",
    user_id: int = Depends(current_user_id),
):
    require_self(path_user_id, user_id)
    user = get_user(path_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return await _load_schedule(user, day_offset, week)


# --------------------------------------------------------------------------- #
# Преподаватели
# --------------------------------------------------------------------------- #


@app.get("/api/teachers")
async def api_teachers(
    q: str = "",
    limit: int = 30,
    refresh: bool = False,
    user_id: int = Depends(current_user_id),
):
    user = get_user(user_id) or {}
    institute = (user.get("institute") or "").strip()
    _, structured = await _catalog()
    if not institute or institute not in structured:
        # Без выбранной группы ищем по всем институтам.
        institutes = [institute] if institute else list(structured)
    else:
        institutes = [institute]

    merged: dict = {}
    for inst in institutes:
        index = await build_teacher_index(structured, inst, force=refresh)
        for name, entry in index.items():
            merged.setdefault(name, entry)

    if (q or "").strip():
        results = search_teachers(merged, q, limit=max(1, min(100, limit)))
    else:
        results = sorted(merged.values(), key=lambda e: e["teacher"])[: max(1, min(100, limit))]

    return {
        "query": q,
        "institute": institute,
        "total": len(merged),
        "results": [
            {
                "teacher": entry["teacher"],
                "subjects": entry["subjects"][:8],
                "groups": entry["groups"][:12],
                "groups_total": len(entry["groups"]),
                "lessons_total": len(entry["lessons"]),
            }
            for entry in results
        ],
    }


@app.get("/api/teachers/{teacher_name}")
async def api_teacher_card(
    teacher_name: str,
    user_id: int = Depends(current_user_id),
):
    user = get_user(user_id) or {}
    institute = (user.get("institute") or "").strip()
    _, structured = await _catalog()
    institutes = [institute] if institute in structured else list(structured)

    for inst in institutes:
        index = await build_teacher_index(structured, inst)
        entry = index.get(teacher_name)
        if entry:
            return {"institute": inst, "teacher": entry}
    raise HTTPException(status_code=404, detail="Преподаватель не найден в расписании")


@app.get("/api/teacher/{path_user_id}")
async def api_teacher_legacy(
    path_user_id: int,
    q: str,
    user_id: int = Depends(current_user_id),
):
    """Совместимость со старой версией mini app."""
    require_self(path_user_id, user_id)
    user = get_user(path_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    _, structured = await _catalog()
    institute = user.get("institute") or ""
    if institute not in structured:
        raise HTTPException(status_code=400, detail="Институт не найден в каталоге")
    index = await build_teacher_index(structured, institute)
    found = []
    for entry in search_teachers(index, q, limit=10):
        for lesson in entry["lessons"][:6]:
            found.append({
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
            })
    return {"query": q, "results": found[:50]}


# --------------------------------------------------------------------------- #
# Избранное
# --------------------------------------------------------------------------- #


@app.get("/api/favorites")
async def api_favorites(user_id: int = Depends(current_user_id)):
    return {"favorites": get_favorites(user_id)}


@app.post("/api/favorites")
async def api_add_favorite(payload: FavoritePayload, user_id: int = Depends(current_user_id)):
    if payload.user_id is not None:
        require_self(payload.user_id, user_id)
    item_type = payload.item_type.strip() or "group"
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Название не может быть пустым")
    add_favorite(user_id, item_type, title, payload.subtitle.strip())
    return {"ok": True, "favorites": get_favorites(user_id)}


@app.delete("/api/favorites/{fav_id}")
async def api_delete_favorite(fav_id: int, user_id: int = Depends(current_user_id)):
    remove_favorite(user_id, fav_id)
    return {"ok": True, "favorites": get_favorites(user_id)}


@app.delete("/api/favorites/{path_user_id}/{fav_id}")
async def api_delete_favorite_legacy(
    path_user_id: int,
    fav_id: int,
    user_id: int = Depends(current_user_id),
):
    require_self(path_user_id, user_id)
    remove_favorite(path_user_id, fav_id)
    return {"ok": True, "favorites": get_favorites(path_user_id)}


# --------------------------------------------------------------------------- #
# Совместимость со старой версией mini app
# --------------------------------------------------------------------------- #


class UserGroupPayload(BaseModel):
    user_id: int
    display_name: str
    api_name: str
    institute: str
    subgroup: str = ""


class SettingsPayload(BaseModel):
    user_id: int
    notif_enabled: int | None = None
    notif_time: str | None = None
    notif_target: int | None = None
    theme: str | None = None
    lead_minutes: int | None = None


@app.get("/api/user/{path_user_id}")
async def api_get_user(path_user_id: int, user_id: int = Depends(current_user_id)):
    require_self(path_user_id, user_id)
    return {"user": get_user_public(path_user_id), "favorites": get_favorites(path_user_id)}


@app.post("/api/user/group")
async def api_set_group(payload: UserGroupPayload, user_id: int = Depends(current_user_id)):
    require_self(payload.user_id, user_id)
    subgroup = payload.subgroup or ""
    if not subgroup and "(" in payload.display_name:
        subgroup = payload.display_name.split("(", 1)[1].rstrip(")").strip()
    save_user(
        user_id,
        build_display_name(payload.api_name, subgroup),
        payload.api_name,
        payload.institute,
        subgroup,
    )
    return {"ok": True, "user": get_user_public(user_id)}


@app.post("/api/user/settings")
async def api_set_settings(payload: SettingsPayload, user_id: int = Depends(current_user_id)):
    require_self(payload.user_id, user_id)
    updates = {}
    if payload.notif_enabled is not None:
        updates["notif_enabled"] = 1 if payload.notif_enabled else 0
    if payload.notif_time is not None:
        updates["notif_time"] = payload.notif_time
    if payload.notif_target is not None:
        updates["notif_target"] = 1 if payload.notif_target else 0
    if payload.theme in ("dark", "light"):
        updates["theme"] = payload.theme
    if payload.lead_minutes is not None:
        updates["lead_minutes"] = max(5, min(60, int(payload.lead_minutes)))
    update_user(user_id, **updates)
    return {"ok": True, "user": get_user_public(user_id)}


# --------------------------------------------------------------------------- #
# Хаб: ЭИОС, новости, объявления
# --------------------------------------------------------------------------- #


@app.get("/api/hub")
async def api_hub():
    return {
        "eios": get_eios_links(),
        "news": get_campus_news(),
        "announcements": get_announcements(),
    }


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=404, content={"detail": "Такого метода нет"})
    return FileResponse(MINIAPP_DIR / "index.html")
