"""
Запись объявлений в базу данных Supabase (таблица public.listings).

Работает параллельно с Google-таблицей: таблица остаётся как была,
база — дополнительное, более удобное хранилище.

Настройка (один раз):
  SUPABASE_URL — адрес проекта, по умолчанию уже прописан ниже.
  SUPABASE_KEY — СЕКРЕТНЫЙ ключ проекта (sb_secret_... или service_role).
                 Supabase → Project Settings → API Keys.
                 В GitHub — через Secret, локально — через переменную окружения.

Если SUPABASE_KEY не задан, запись в базу просто пропускается с
предупреждением — остальная работа скрипта (таблица) не ломается.

Дубли не создаются: пара (source, external_id) уникальна, повторная
вставка того же объявления молча игнорируется.
"""

import os
import re

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://vbkrmzjmmbhtnqjotrnz.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
TABLE = "listings"

COLUMNS = {
    "source", "source_type", "external_id", "listing_key", "link", "title", "company",
    "address", "district", "postcode", "housing_type", "rooms", "area_m2", "kalt", "neben",
    "heiz", "warm", "wbs", "jobcenter_note", "distance_km", "distance_src", "priority",
    "status", "comment", "published_at", "red_flags", "raw",
}
NUMERIC = {"rooms", "area_m2", "kalt", "neben", "heiz", "warm", "distance_km"}

_warned = False


def to_number(v):
    """'1.234,50 €' / '650' / 2.5 / '' -> float или None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    m = re.search(r"\d[\d.\s]*(?:,\d+)?|\d+(?:\.\d+)?", s)
    if not m:
        return None
    t = m.group(0).replace(" ", "")
    if "," in t:                      # немецкий формат: 1.234,50
        t = t.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", t):   # 1.234 = тысяча
        t = t.replace(".", "")
    try:
        return float(t)
    except ValueError:
        return None


def _clean(rec: dict) -> dict:
    out = {}
    for k, v in rec.items():
        if k not in COLUMNS:
            continue
        if k in NUMERIC:
            v = to_number(v)
        elif isinstance(v, str):
            v = v.strip() or None
        out[k] = v
    out["source"] = out.get("source") or "unknown"
    out["external_id"] = str(out.get("external_id") or out.get("link") or "")
    return out


def insert_listings(records: list[dict]) -> int:
    """Вставляет объявления в базу. Никогда не бросает исключение наружу —
    ошибка базы не должна мешать записи в Google-таблицу.
    Возвращает количество отправленных записей (0 при ошибке/пропуске)."""
    global _warned
    if not records:
        return 0
    if not SUPABASE_KEY:
        if not _warned:
            print("[warn] SUPABASE_KEY не задан — запись в базу Supabase пропущена")
            _warned = True
        return 0

    # PostgREST требует одинаковый набор ключей у всех строк в пакете
    rows = [_clean(r) for r in records if (r.get("external_id") or r.get("link"))]
    keys = set().union(*(r.keys() for r in rows)) if rows else set()
    rows = [{k: r.get(k) for k in keys} for r in rows]
    if not rows:
        return 0

    try:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/{TABLE}",
            params={"on_conflict": "source,external_id"},
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=ignore-duplicates,return=minimal",
            },
            json=rows,
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"[error] Supabase недоступен: {exc}")
        return 0
    if not resp.ok:
        print(f"[error] Supabase вернул {resp.status_code}: {resp.text[:500]}")
        return 0
    print(f"[db] Supabase: отправлено {len(rows)} записей")
    return len(rows)
