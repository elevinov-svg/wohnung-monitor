"""
Расстояние от квартиры до Boxhagener Platz (главный ориентир поиска).

Порядок определения координат:
  1. координаты уже есть в объявлении (HOWOGE, inberlinwohnen) — берём их;
  2. иначе адрес -> координаты через OpenStreetMap Nominatim (бесплатно,
     не чаще 1 запроса в секунду), результат кэшируется в geocache.json;
  3. если адрес не нашёлся — грубая оценка по почтовому индексу (PLZ_NEAR).

Модуль общий: его можно использовать для любого источника (Kleinanzeigen,
ImmoScout и т.д.) — нужен только адрес или хотя бы почтовый индекс.
"""

import json
import math
import re
import time
from pathlib import Path

import requests

BOXHAGENER_PLATZ = (52.51065, 13.46160)

CACHE_FILE = Path(__file__).parent / "geocache.json"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_UA = "wohnung-monitor/1.0 (personal apartment search; github.com/elevinov-svg)"

# Индексы вокруг Boxhagener Platz и примерное расстояние от их центра, км.
# Используются только если по адресу координаты найти не удалось.
PLZ_NEAR = {
    "10243": 1.8, "10245": 0.9, "10247": 0.6, "10249": 1.6,   # Friedrichshain
    "10317": 1.8, "10365": 2.4, "10367": 2.9, "10369": 3.4,   # Lichtenberg / Rummelsburg
    "10997": 2.5, "10999": 3.3, "10961": 4.2, "10967": 3.9,   # Kreuzberg
    "12435": 2.4, "12437": 3.8,                               # Alt-Treptow / Baumschulenweg
    "10179": 3.2, "10178": 3.6, "10119": 4.3,                 # Mitte
    "10407": 3.2, "10405": 3.9,                               # Prenzlauer Berg (восток)
    "12047": 3.6, "12045": 3.6, "12059": 3.3,                 # Neukölln (север)
}

_cache = None
_last_call = 0.0


def haversine_km(a, b) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        _cache = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
    return _cache


def save_cache() -> None:
    if _cache is not None:
        CACHE_FILE.write_text(json.dumps(_cache, ensure_ascii=False, indent=1, sort_keys=True),
                              encoding="utf-8")


def _clean_address(address: str) -> str:
    a = re.sub(r"\s+", " ", address or "").strip()
    a = re.sub(r"Berlin/[\wäöüß-]+", "Berlin", a)       # 'Berlin/Pankow' -> 'Berlin'
    a = re.sub(r"\bStr\.", "Straße", a)
    if "berlin" not in a.lower():
        a += ", Berlin"
    return a


def geocode(address: str):
    """Адрес -> (lat, lon) или None. С кэшем и паузой 1 с между запросами."""
    global _last_call
    if not address:
        return None
    q = _clean_address(address)
    cache = _load_cache()
    if q in cache:
        return tuple(cache[q]) if cache[q] else None
    wait = 1.1 - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    try:
        r = requests.get(NOMINATIM, params={"q": q, "format": "json", "limit": 1, "countrycodes": "de"},
                         headers={"User-Agent": NOMINATIM_UA}, timeout=20)
        _last_call = time.time()
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] geocode не удался для '{q}': {exc}")
        return None  # не кэшируем ошибку — попробуем в следующий раз
    coords = (float(data[0]["lat"]), float(data[0]["lon"])) if data else None
    cache[q] = list(coords) if coords else None
    return coords


def distance_to_boxi(item: dict):
    """Возвращает (км, способ) или (None, None). Способ: 'коорд.', 'адрес', 'индекс'."""
    if item.get("lat") and item.get("lon"):
        return round(haversine_km(BOXHAGENER_PLATZ, (float(item["lat"]), float(item["lon"]))), 1), "коорд."
    coords = geocode(item.get("address") or "")
    if coords:
        return round(haversine_km(BOXHAGENER_PLATZ, coords), 1), "адрес"
    plz = item.get("postcode")
    if plz in PLZ_NEAR:
        return PLZ_NEAR[plz], "индекс"
    return None, None
