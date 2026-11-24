import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"


def _load_env_file():
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('\"').strip("'"))


_load_env_file()

_data_dir = Path(os.getenv("DATA_DIR", "data").strip() or "data").expanduser()
DATA_DIR = _data_dir if _data_dir.is_absolute() else (BASE_DIR / _data_dir).resolve()
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "sfu_student.sqlite3"

SFU_TZ = ZoneInfo(os.getenv("SFU_TZ", "Asia/Krasnoyarsk"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def get_conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _columns(con, table: str) -> set:
    return {row["name"] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_schema(con):
    cols = _columns(con, "users")
    additions = {
        "theme": "TEXT NOT NULL DEFAULT 'dark'",
        "lead_minutes": "INTEGER NOT NULL DEFAULT 15",
        "updated_at": "TEXT",
        "subgroup": "TEXT NOT NULL DEFAULT ''",
        "first_name": "TEXT NOT NULL DEFAULT ''",
        "username": "TEXT NOT NULL DEFAULT ''",
    }
    for column, ddl in additions.items():
        if column not in cols:
            con.execute(f"ALTER TABLE users ADD COLUMN {column} {ddl}")
    # Профиль, созданный до появления подгруппы, восстанавливаем из display_name.
    con.execute(
        """
        UPDATE users
        SET subgroup = TRIM(SUBSTR(display_name, INSTR(display_name, '(') + 1,
                                    INSTR(display_name, ')') - INSTR(display_name, '(') - 1))
        WHERE subgroup = '' AND display_name LIKE '%(%)%'
        """
    )


def init_db():
    with get_conn() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                display_name TEXT NOT NULL,
                api_name TEXT NOT NULL,
                institute TEXT NOT NULL,
                notif_enabled INTEGER NOT NULL DEFAULT 0,
                notif_time TEXT NOT NULL DEFAULT '08:00',
                notif_target INTEGER NOT NULL DEFAULT 1,
                theme TEXT NOT NULL DEFAULT 'dark',
                lead_minutes INTEGER NOT NULL DEFAULT 15,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                subgroup TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT ''
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                item_type TEXT NOT NULL,
                title TEXT NOT NULL,
                subtitle TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(user_id, item_type, title)
            )
            """
        )
        _migrate_schema(con)


def build_display_name(api_name: str, subgroup: str = "") -> str:
    """Единый формат подписи группы и в боте, и в mini app."""
    api_name = (api_name or "").strip()
    subgroup = (subgroup or "").strip()
    if subgroup and subgroup not in ("1 подгруппа", "—"):
        return f"{api_name} ({subgroup})"
    return api_name


def get_user(user_id: int):
    init_db()
    with get_conn() as con:
        row = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_public(user_id: int):
    """Профиль без служебных полей — для ответов API."""
    user = get_user(user_id)
    if not user:
        return None
    return {
        "user_id": user["user_id"],
        "display_name": user["display_name"],
        "api_name": user["api_name"],
        "institute": user["institute"],
        "subgroup": user.get("subgroup") or "",
        "first_name": user.get("first_name") or "",
        "username": user.get("username") or "",
        "notif_enabled": user["notif_enabled"],
        "notif_time": user["notif_time"],
        "notif_target": user["notif_target"],
        "theme": user["theme"],
        "lead_minutes": user["lead_minutes"],
        "has_group": bool(user["api_name"]),
    }


def save_user(user_id: int, display_name: str, api_name: str, institute: str, subgroup: str = "", **extra):
    init_db()
    now_iso = datetime.now(SFU_TZ).isoformat()
    fields = {"first_name": "", "username": ""}
    fields.update({k: v for k, v in extra.items() if k in ("first_name", "username")})
    with get_conn() as con:
        con.execute(
            """
            INSERT INTO users(user_id, display_name, api_name, institute, subgroup, first_name, username,
                              created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                display_name=excluded.display_name,
                api_name=excluded.api_name,
                institute=excluded.institute,
                subgroup=excluded.subgroup,
                first_name=CASE WHEN excluded.first_name != '' THEN excluded.first_name ELSE users.first_name END,
                username=CASE WHEN excluded.username != '' THEN excluded.username ELSE users.username END,
                updated_at=excluded.updated_at
            """,
            (
                user_id,
                display_name,
                api_name,
                institute,
                subgroup,
                fields["first_name"],
                fields["username"],
                now_iso,
                now_iso,
            ),
        )


def update_user(user_id: int, **fields):
    init_db()
    allowed = {
        "notif_enabled",
        "notif_time",
        "notif_target",
        "theme",
        "lead_minutes",
        "display_name",
        "api_name",
        "institute",
        "subgroup",
        "first_name",
        "username",
    }
    clean = {k: v for k, v in fields.items() if k in allowed}
    if not clean:
        return
    clean["updated_at"] = datetime.now(SFU_TZ).isoformat()
    sql = ", ".join(f"{k}=?" for k in clean)
    with get_conn() as con:
        con.execute(f"UPDATE users SET {sql} WHERE user_id=?", (*clean.values(), user_id))


def ensure_user(user_id: int, first_name: str = "", username: str = ""):
    """Гарантирует запись пользователя (нужно для настроек и уведомлений).

    Группа при этом остаётся пустой: студент выбирает её сам в боте или mini app.
    """
    user = get_user(user_id)
    if user:
        if (first_name and not user.get("first_name")) or (username and not user.get("username")):
            update_user(
                user_id,
                first_name=first_name or user.get("first_name") or "",
                username=username or user.get("username") or "",
            )
        return get_user(user_id)
    init_db()
    now_iso = datetime.now(SFU_TZ).isoformat()
    with get_conn() as con:
        con.execute(
            """
            INSERT INTO users(user_id, display_name, api_name, institute, subgroup, first_name, username,
                              created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (user_id, "", "", "", "", first_name, username, now_iso, now_iso),
        )
    return get_user(user_id)


def has_group(user) -> bool:
    return bool(user and (user.get("api_name") or "").strip())


def get_favorites(user_id: int):
    init_db()
    with get_conn() as con:
        rows = con.execute(
            "SELECT * FROM favorites WHERE user_id=? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def add_favorite(user_id: int, item_type: str, title: str, subtitle: str = ""):
    init_db()
    now_iso = datetime.now(SFU_TZ).isoformat()
    with get_conn() as con:
        con.execute(
            """
            INSERT OR IGNORE INTO favorites(user_id, item_type, title, subtitle, created_at)
            VALUES(?,?,?,?,?)
            """,
            (user_id, item_type, title, subtitle, now_iso),
        )


def remove_favorite(user_id: int, fav_id: int):
    init_db()
    with get_conn() as con:
        con.execute("DELETE FROM favorites WHERE user_id=? AND id=?", (user_id, fav_id))
