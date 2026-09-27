#!/usr/bin/env python3
"""
Фильтр-скрипт: читает все объявления из Supabase, фильтрует по критериям
(исключает WG/Zwischenmiete, проверяет расстояние до Boxhagener Platz),
пишет подходящие в Google Sheet.

Запускается по расписанию (systemd timer на сервере).
Состояние: какие объявления уже записаны в Sheet — в filter_state.json.
"""

import datetime
import json
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import geo
from sheets import append_rows

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "filter_state.json"
BERLIN = ZoneInfo("Europe/Berlin")

# Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://vbkrmzjmmbhtnqjotrnz.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Критерии фильтрации
MAX_DISTANCE_KM = 3.5  # от Boxhagener Platz
MAX_KALT = 850  # примерная оценка, не официальный лимит
MAX_ROOMS = 2  # 1 комната или студия ок

# AV Wohnen Berlin 2026, 1 человек
JC_BRUTTOKALT = 449.0
JC_BRUTTOKALT_MAX = round(JC_BRUTTOKALT * 1.2, 2)


def now_berlin() -> datetime.datetime:
    return datetime.datetime.now(BERLIN).replace(microsecond=0)


def load_state() -> set:
    """Возвращает set из listing_key объявлений, уже записанных в Sheet."""
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("written_keys", []))
    return set()


def save_state(written_keys: set) -> None:
    STATE_FILE.write_text(
        json.dumps({"written_keys": sorted(written_keys)}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def fetch_all_listings() -> list[dict]:
    """Читает все объявления из Supabase."""
    if not SUPABASE_KEY:
        print("[error] SUPABASE_KEY не задан")
        sys.exit(1)

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }

    # Читаем все записи (Supabase по умолчанию возвращает до 1000, но можем увеличить)
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/listings",
        headers=headers,
        params={"select": "*", "order": "found_at.desc", "limit": 5000},
        timeout=30,
    )
    if not resp.ok:
        print(f"[error] Supabase вернул {resp.status_code}: {resp.text[:500]}")
        sys.exit(1)

    listings = resp.json()
    print(f"[info] Прочитано {len(listings)} объявлений из Supabase")
    return listings


def is_wg_or_zwischenmiete(listing: dict) -> bool:
    """Проверяет, является ли объявление WG или Zwischenmiete."""
    title = (listing.get("title") or "").lower()
    raw = (listing.get("raw") or "").lower()
    address = (listing.get("address") or "").lower()

    text = f"{title} {raw} {address}"

    # WG паттерны
    wg_patterns = [
        r"\bwg\b",
        r"wohngemeinschaft",
        r"mitbewohner",
        r"zimmer in",
        r"\d+er.?wg",
    ]

    # Zwischenmiete паттерны
    zwischen_patterns = [
        r"zwischenmiete",
        r"untermiete",
        r"befristet",
        r"temporary",
        r"short.?term",
        r"\d+\s*monat",  # "3 Monate", "6 Monate"
    ]

    for pattern in wg_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True

    for pattern in zwischen_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True

    return False


def has_wbs_mention(listing: dict) -> bool:
    """Проверяет упоминание WBS в тексте."""
    wbs_field = listing.get("wbs")
    if wbs_field and wbs_field.lower() in ["да", "yes", "required", "erforderlich"]:
        return True

    title = (listing.get("title") or "").lower()
    raw = (listing.get("raw") or "").lower()
    text = f"{title} {raw}"

    return "wbs" in text or "wohnberechtigungsschein" in text


def calculate_distance(listing: dict) -> tuple[float | None, str]:
    """Считает расстояние до Boxhagener Platz.
    Возвращает (km, source) где source = 'geo' или 'postcode'."""

    # Если в базе уже есть distance_km — используем
    if listing.get("distance_km") is not None:
        return listing["distance_km"], listing.get("distance_src") or "cached"

    # Иначе считаем через geo.py
    km, how = geo.distance_to_boxi(listing)
    return km, how


def passes_filters(listing: dict) -> tuple[bool, str, float | None, str]:
    """
    Проверяет объявление по всем фильтрам.
    Возвращает (passes, reason, distance_km, distance_src).
    """

    # 1. WG/Zwischenmiete
    if is_wg_or_zwischenmiete(listing):
        return False, "WG или Zwischenmiete", None, ""

    # 2. Комнаты
    rooms = listing.get("rooms")
    if rooms is not None and rooms > MAX_ROOMS:
        return False, f"Слишком много комнат ({rooms} > {MAX_ROOMS})", None, ""

    # 3. Kaltmiete (примерная оценка)
    kalt = listing.get("kalt")
    if kalt is not None and kalt > MAX_KALT:
        return False, f"Kaltmiete слишком высокая ({kalt} > {MAX_KALT})", None, ""

    # 4. Расстояние до Boxhagener Platz
    km, src = calculate_distance(listing)
    if km is None:
        return False, "Не удалось определить расстояние", None, ""

    if km > MAX_DISTANCE_KM:
        return False, f"Слишком далеко ({km:.1f} км > {MAX_DISTANCE_KM} км)", km, src

    # Всё ок
    return True, "OK", km, src


def jobcenter_note(listing: dict) -> str:
    """Формирует заметку о соответствии лимиту Jobcenter."""
    kalt = listing.get("kalt")
    neben = listing.get("neben")
    warm = listing.get("warm")
    bruttokalt = listing.get("bruttokalt")

    if bruttokalt is not None:
        bk = bruttokalt
    elif kalt is not None and neben is not None:
        bk = kalt + neben
    else:
        bk = None

    if bk is not None:
        if bk <= JC_BRUTTOKALT:
            return f"✓ В лимите (Bruttokalt {bk:.0f} ≤ {JC_BRUTTOKALT:.0f})"
        if bk <= JC_BRUTTOKALT_MAX:
            return f"⚠ Надбавка (Bruttokalt {bk:.0f}, лимит {JC_BRUTTOKALT:.0f}+20%)"
        return f"✗ Выше лимита (Bruttokalt {bk:.0f})"

    if warm is not None:
        return f"? Проверить: Warmmiete {warm:.0f}"
    if kalt is not None:
        return f"? Проверить: Kaltmiete {kalt:.0f}, Nebenkosten неизвестны"

    return "? Нужно проверить"


def fmt_num(v) -> str:
    """Форматирует число для Sheet."""
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else f"{v:.2f}".replace(".", ",")


def distance_label(km: float | None, src: str) -> str:
    """Форматирует расстояние для Sheet."""
    if km is None:
        return ""
    approx = "~" if src == "postcode" else ""
    return f"{approx}{km:.1f}".replace(".", ",") + " км"


def priority_label(km: float | None) -> str:
    """Приоритет: зелёный (<= 2 км), жёлтый (> 2 км)."""
    if km is None:
        return ""
    return "🟢 рядом" if km <= 2.0 else "🟡 в радиусе"


def build_sheet_row(listing: dict, distance_km: float | None, distance_src: str) -> list:
    """
    Строка для Sheet. Колонки:
    ID, Дата обнаружения, Источник, Ссылка, Район, Адрес, Тип жилья,
    Комнаты, Площадь (м2), Kaltmiete, Nebenkosten, Heizkosten, Warmmiete,
    WBS, Jobcenter, Расстояние, Контакт, Приоритет, Статус,
    Комментарий, Дата обновления.
    """
    ts = now_berlin().strftime("%Y-%m-%d %H:%M")
    source = listing.get("source") or ""
    company = listing.get("company") or ""

    wbs = listing.get("wbs") or ""
    if has_wbs_mention(listing) and not wbs:
        wbs = "да (из текста)"

    comment = "Автофильтр из Supabase"
    if has_wbs_mention(listing):
        comment += " | WBS упомянут"

    return [
        listing.get("listing_key") or listing.get("external_id") or "",  # ID
        ts,  # Дата обнаружения
        source,  # Источник
        listing.get("link") or "",  # Ссылка
        listing.get("district") or "",  # Район
        listing.get("address") or "",  # Адрес
        listing.get("housing_type") or "Wohnung",  # Тип жилья
        fmt_num(listing.get("rooms")),  # Комнаты
        fmt_num(listing.get("area_m2")),  # Площадь
        fmt_num(listing.get("kalt")),  # Kaltmiete
        fmt_num(listing.get("neben")),  # Nebenkosten
        fmt_num(listing.get("heiz")),  # Heizkosten
        fmt_num(listing.get("warm")),  # Warmmiete
        wbs,  # WBS
        jobcenter_note(listing),  # Jobcenter
        distance_label(distance_km, distance_src),  # Расстояние
        company or source,  # Контакт
        priority_label(distance_km),  # Приоритет
        listing.get("status") or "",  # Статус
        comment,  # Комментарий
        ts,  # Дата обновления
    ]


def main():
    print(f"=== Фильтр-скрипт wohnung-monitor === {now_berlin()}")

    # Загружаем состояние
    written_keys = load_state()
    print(f"[info] Уже записано в Sheet: {len(written_keys)} объявлений")

    # Читаем все объявления из Supabase
    listings = fetch_all_listings()

    # Фильтруем
    new_rows = []
    filtered_out = {"wg_zwischen": 0, "rooms": 0, "price": 0, "distance": 0, "no_distance": 0}

    for listing in listings:
        key = listing.get("listing_key") or listing.get("external_id")
        if not key:
            continue

        # Уже записано?
        if key in written_keys:
            continue

        # Фильтры
        passes, reason, dist_km, dist_src = passes_filters(listing)

        if not passes:
            if "WG" in reason or "Zwischenmiete" in reason:
                filtered_out["wg_zwischen"] += 1
            elif "комнат" in reason:
                filtered_out["rooms"] += 1
            elif "Kaltmiete" in reason:
                filtered_out["price"] += 1
            elif "далеко" in reason:
                filtered_out["distance"] += 1
            elif "расстояние" in reason:
                filtered_out["no_distance"] += 1
            continue

        # Прошло фильтр
        row = build_sheet_row(listing, dist_km, dist_src)
        new_rows.append((key, row))
        print(f"[new] {listing.get('source')}: {listing.get('address')} — {dist_km:.1f} км")

    # Пишем в Sheet
    if new_rows:
        try:
            rows_only = [row for _, row in new_rows]
            append_rows(rows_only)
            print(f"[success] Записано в Sheet: {len(new_rows)} новых объявлений")

            # Обновляем состояние
            for key, _ in new_rows:
                written_keys.add(key)
            save_state(written_keys)

        except Exception as exc:
            print(f"[error] Не удалось записать в Sheet: {exc}")
            return 1
    else:
        print("[info] Нет новых объявлений для записи в Sheet")

    # Статистика фильтрации
    print(f"[stats] Отфильтровано: WG/Zwischen={filtered_out['wg_zwischen']}, "
          f"комнат={filtered_out['rooms']}, цена={filtered_out['price']}, "
          f"далеко={filtered_out['distance']}, нет координат={filtered_out['no_distance']}")

    geo.save_cache()
    return 0


if __name__ == "__main__":
    sys.exit(main())
