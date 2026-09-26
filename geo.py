"""
Расстояние от квартиры до Boxhagener Platz.

Порядок:
  1. координаты уже есть в объявлении (HOWOGE, GESOBAU, Gewobag) — берём их;
  2. иначе адрес -> координаты через OpenStreetMap Nominatim (не чаще 1 запроса
     в секунду), результат кэшируется (Supabase, таблица geocache);
  3. адрес не нашёлся — грубая оценка по почтовому индексу (PLZ_NEAR).

Временная ошибка геокодера (сеть, 429, 5xx…) — это GeoTempError: объявление
не считается обработанным, в следующем цикле пробуем снова.
"""

import math
import re
import time

import requests

BOXHAGENER_PLATZ = (52.51065, 13.46160)
NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_UA = "wohnung-monitor/2.0 (personal apartment search; github.com/elevinov-svg)"

# Индексы вокруг Boxhagener Platz и примерное расстояние от их центра, км.
PLZ_NEAR = {
    "10243": 1.8, "10245": 0.9, "10247": 0.6, "10249": 1.6,   # Friedrichshain
    "10317": 1.8, "10365": 2.4, "10367": 2.9, "10369": 3.4,   # Lichtenberg / Rummelsburg
    "10997": 2.5, "10999": 3.3, "10961": 4.2, "10967": 3.9,   # Kreuzberg
    "12435": 2.4, "12437": 3.8,                               # Alt-Treptow / Baumschulenweg
    "10179": 3.2, "10178": 3.6, "10119": 4.3,                 # Mitte
    "10407": 3.2, "10405": 3.9,                               # Prenzlauer Berg (восток)
    "12047": 3.6, "12045": 3.6, "12059": 3.3,                 # Neukölln (север)
}


class GeoTempError(Exception):
    """Геокодер временно недоступен — повторить позже."""


def haversine_km(a, b) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def clean_address(address: str) -> str:
    a = re.sub(r"\s+", " ", address or "").strip()
    a = re.sub(r"Berlin/[\wäöüß-]+", "Berlin", a)        # 'Berlin/Pankow' -> 'Berlin'
    a = re.sub(r"\bStr\.", "Straße", a)                  # 'Köpenicker Str.' -> 'Köpenicker Straße'
    a = re.sub(r"(?<=[a-zäöüß])str\.", "straße", a)      # 'Thomasstr.' -> 'Thomasstraße'
    a = re.sub(r"(?<=[Ss])trasse\b", "traße", a)         # WBM пишет 'Strasse'
    if "berlin" not in a.lower():
        a += ", Berlin"
    return a


def street_only(address: str, postcode: str | None) -> str | None:
    """'Arnbrucker Straße 1, 10318 Berlin' -> 'Arnbrucker Straße, 10318 Berlin'."""
    street = re.split(r",", address or "")[0]
    bare = re.sub(r"\s+\d+\s*[a-zA-Z]?(?:\s*[-–]\s*\d+\s*[a-zA-Z]?)?\s*$", "", street).strip()
    if not bare or bare == street.strip():
        return None
    return f"{bare}, {postcode + ' ' if postcode else ''}Berlin"


class Geocoder:
    def __init__(self, cache: dict | None = None):
        # cache: запрос -> (lat, lon) или None («точно не найдено»)
        self.cache = dict(cache or {})
        self.new_entries: dict = {}
        self._last_call = 0.0

    def geocode(self, address: str):
        """Адрес -> (lat, lon) | None. Бросает GeoTempError при временной ошибке."""
        if not address:
            return None
        q = clean_address(address)
        if q in self.cache:
            return self.cache[q]
        wait = 1.1 - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get(NOMINATIM, params={"q": q, "format": "json", "limit": 1, "countrycodes": "de"},
                             headers={"User-Agent": NOMINATIM_UA}, timeout=20)
        except requests.RequestException as exc:
            raise GeoTempError(f"сеть: {exc}") from exc
        finally:
            self._last_call = time.time()
        if not r.ok:   # 429, 5xx, блокировка — всё считаем временным
            raise GeoTempError(f"Nominatim HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as exc:
            raise GeoTempError("Nominatim вернул не JSON") from exc
        coords = (float(data[0]["lat"]), float(data[0]["lon"])) if data else None
        self.cache[q] = coords
        self.new_entries[q] = coords
        return coords

    def distance(self, x, allow_temp_error: bool = True):
        """-> (км, способ) или (None, None). Способ: 'коорд.', 'адрес', 'улица', 'индекс'.
        allow_temp_error=False — вместо GeoTempError сразу запасной вариант по индексу."""
        if x.lat is not None and x.lon is not None:
            return round(haversine_km(BOXHAGENER_PLATZ, (float(x.lat), float(x.lon))), 1), "коорд."
        try:
            coords = self.geocode(x.address or "")
        except GeoTempError:
            if allow_temp_error:
                raise
            coords = None
        if coords:
            x.lat, x.lon = coords
            return round(haversine_km(BOXHAGENER_PLATZ, coords), 1), "адрес"
        street = street_only(x.address or "", x.postcode)
        if street:   # новостройки: дома ещё нет в OSM, а улица есть
            try:
                coords = self.geocode(street)
            except GeoTempError:
                if allow_temp_error:
                    raise
                coords = None
            if coords:
                return round(haversine_km(BOXHAGENER_PLATZ, coords), 1), "улица"
        if x.postcode in PLZ_NEAR:
            return PLZ_NEAR[x.postcode], "индекс"
        return None, None
