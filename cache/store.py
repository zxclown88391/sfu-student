import json
import re
from datetime import datetime
from database.db import CACHE_DIR, SFU_TZ


def cache_path(key: str):
    safe = re.sub(r"[^0-9A-Za-zА-Яа-я_-]", "_", key)[:150]
    return CACHE_DIR / f"{safe}.json"


def read_cache(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("saved_at"), data.get("data")
    except Exception:
        return None, None


def write_cache(path, data):
    temp = path.with_suffix(".tmp")
    payload = {
        "saved_at": datetime.now(SFU_TZ).isoformat(),
        "data": data,
    }
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)
