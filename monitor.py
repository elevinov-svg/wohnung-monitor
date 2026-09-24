#!/usr/bin/env python3
"""
Wohnung-Monitor: проверяет источники объявлений о квартирах и складывает
новые подходящие объявления во вкладку "Объявления" Google-таблицы.

Запускается периодически (см. .github/workflows/monitor.yml).
Состояние хранится рядом со скриптом:
  seen.json    — что мы уже видели (чтобы не писать дубли в таблицу)
  timing.json  — когда каждая квартира впервые появилась на сайте компании
                 и на портале inberlinwohnen.de (для замера задержки портала)

Типы источников (см. sources.yaml):
  rss         — RSS/Atom-фид
  html_links  — простая страница, ищем ссылки по regex
  company     — сайт городской жилищной компании (парсеры в landeseigene.py)
"""

import datetime
import json
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

import geo
from landeseigene import COMPANY_CHECKERS
from sheets import append_rows

BASE_DIR = Path(__file__).parent
SOURCES_FILE = BASE_DIR / "sources.yaml"
SEEN_FILE = BASE_DIR / "seen.json"
TIMING_FILE = BASE_DIR / "timing.json"
BERLIN = ZoneInfo("Europe/Berlin")

# Telegram пока не используем (см. README) — код оставлен рабочим,
# чтобы можно было включить обратно одной строкой, когда понадобится.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
USE_TELEGRAM = os.environ.get("USE_TELEGRAM", "false").lower() == "true"

# Разовая самопроверка записи в таблицу (workflow_dispatch input "self_test").
SELF_TEST_SHEETS = os.environ.get("SELF_TEST_SHEETS", "false").lower() == "true"
# Прогон без записи в таблицу и без сохранения состояния — только печать.
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# AV Wohnen Berlin 2026, 1 человек: Richtwert Bruttokaltmiete (Kaltmiete + kalte
# Nebenkosten, без отопления). +10% соцжильё, до +20% для бездомных/Neuanmietung.
# Перед подачей заявки перепроверять по официальному источнику.
JC_BRUTTOKALT = 449.0
JC_BRUTTOKALT_MAX = round(JC_BRUTTOKALT * 1.2, 2)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def now_berlin() -> datetime.datetime:
    return datetime.datetime.now(BERLIN).replace(microsecond=0)


def load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[warn] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы:\n" + text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                                    "parse_mode": "HTML"}, timeout=20)
    if not resp.ok:
        print(f"[error] Telegram API вернул {resp.status_code}: {resp.text}")


# ----------------------------------------------------------------- простые источники

def check_rss(source: dict) -> list[dict]:
    feed = feedparser.parse(source["url"], request_headers=HEADERS)
    items = []
    for entry in feed.entries:
        uid = entry.get("id") or entry.get("link")
        if uid:
            items.append({"id": uid, "title": entry.get("title", "(без заголовка)"),
                          "link": entry.get("link", source["url"])})
    return items


def check_html_links(source: dict) -> list[dict]:
    resp = requests.get(source["url"], headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    pattern = re.compile(source["link_pattern"])
    items, seen_links = [], set()
    for a in soup.find_all("a", href=True):
        if pattern.search(a["href"]):
            full_url = requests.compat.urljoin(source["url"], a["href"])
            if full_url not in seen_links:
                seen_links.add(full_url)
                items.append({"id": full_url, "title": a.get_text(strip=True) or full_url,
                              "link": full_url})
    return items


def check_company(source: dict) -> list[dict]:
    return COMPANY_CHECKERS[source["company"]]()


CHECKERS = {"rss": check_rss, "html_links": check_html_links, "company": check_company}


# ----------------------------------------------------------------- фильтр

def passes_filters(it: dict, f: dict) -> bool:
    """Отсеиваем явно неподходящее. Если поле неизвестно — НЕ отсеиваем."""
    if not f:
        return True
    if f.get("berlin_only"):
        plz = it.get("postcode")
        addr = (it.get("address") or "").lower()
        if plz and not ("10115" <= plz <= "14199"):
            return False
        if not plz and addr and "berlin" not in addr and it.get("district") == "Brandenburg":
            return False
    rooms = it.get("rooms")
    if rooms is not None and f.get("max_rooms") is not None and rooms > f["max_rooms"]:
        return False
    if rooms is not None and f.get("min_rooms") is not None and rooms < f["min_rooms"]:
        return False
    warm, kalt = it.get("warm"), it.get("kalt")
    if warm is not None and f.get("max_warm") is not None and warm > f["max_warm"]:
        return False
    if warm is None and kalt is not None and f.get("max_kalt") is not None and kalt > f["max_kalt"]:
        return False
    return True


def passes_distance(it: dict, f: dict) -> bool:
    """Фильтр по расстоянию до Boxhagener Platz. Считаем только для квартир,
    уже прошедших остальные фильтры (чтобы не дёргать геокодер лишний раз)."""
    max_km = f.get("max_km_boxhagener_platz")
    if not max_km:
        return True
    km, how = geo.distance_to_boxi(it)
    it["dist_km"], it["dist_src"] = km, how
    if km is None:
        print(f"[info] не удалось определить место: {it.get('address')} — пропускаю")
        return False
    return km <= max_km


def distance_label(it: dict) -> str:
    km = it.get("dist_km")
    if km is None:
        return ""
    approx = "~" if it.get("dist_src") == "индекс" else ""
    return f"{approx}{km:.1f}".replace(".", ",") + " км до Boxhagener Pl."


def priority_label(it: dict, f: dict) -> str:
    km = it.get("dist_km")
    if km is None:
        return ""
    return "🟢 рядом" if km <= f.get("green_km", 2.0) else "🟡 в радиусе"


def jobcenter_note(it: dict) -> str:
    kalt, neben, warm = it.get("kalt"), it.get("neben"), it.get("warm")
    if kalt is not None and neben is not None:
        bk = round(kalt + neben, 2)
        if bk <= JC_BRUTTOKALT:
            return f"потенциально в лимите (Bruttokalt {bk:.0f} ≤ {JC_BRUTTOKALT:.0f})"
        if bk <= JC_BRUTTOKALT_MAX:
            return f"только с надбавкой (Bruttokalt {bk:.0f}, лимит {JC_BRUTTOKALT:.0f}+20%)"
        return f"выше лимита (Bruttokalt {bk:.0f})"
    if warm is not None:
        return f"проверить: известна только Warmmiete {warm:.0f}"
    if kalt is not None:
        return f"проверить: Kaltmiete {kalt:.0f}, Nebenkosten неизвестны"
    return "нужно проверить"


def fmt_num(v) -> str:
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else f"{v:.2f}".replace(".", ",")


# ----------------------------------------------------------------- строка таблицы

def build_sheet_row(item: dict, source_name: str, filters: dict | None = None) -> list:
    """
    Колонки вкладки "Объявления":
    ID, Дата обнаружения, Источник, Ссылка, Район, Адрес, Тип жилья,
    Комнаты, Площадь (м2), Kaltmiete, Nebenkosten, Heizkosten, Warmmiete,
    WBS, Jobcenter, До Ostkreuz, Контакт, Приоритет, Статус,
    Комментарий AI, Дата обновления.
    """
    ts = now_berlin().strftime("%Y-%m-%d %H:%M")
    title = item.get("title", "") or ""
    is_company = item.get("company") is not None

    rooms = item.get("rooms")
    if rooms is None:
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*[- ]?Zimmer", title, re.IGNORECASE)
        rooms = m.group(1) if m else None

    comment = "Добавлено автоматически, требует проверки"
    if is_company:
        comment = f"{title} | " + comment
        if item.get("published"):
            comment += f" | на портале с {item['published']}"

    return [
        "",                                         # ID
        ts,                                         # Дата обнаружения (Берлин)
        source_name,                                # Источник
        item["link"],                               # Ссылка
        item.get("district") or "",                 # Район
        item.get("address") or title,               # Адрес
        "Wohnung" if is_company else "",            # Тип жилья
        fmt_num(rooms) if isinstance(rooms, (int, float)) else (rooms or ""),
        fmt_num(item.get("area")),                  # Площадь
        fmt_num(item.get("kalt")),                  # Kaltmiete
        fmt_num(item.get("neben")),                 # Nebenkosten
        fmt_num(item.get("heiz")),                  # Heizkosten
        fmt_num(item.get("warm")),                  # Warmmiete
        item.get("wbs") or ("неизвестно" if is_company else ""),
        jobcenter_note(item) if is_company else "",
        distance_label(item),                       # «До Ostkreuz» — км до Boxhagener Platz
        item.get("company") or "",                  # Контакт — кто сдаёт
        priority_label(item, filters or {}),        # Приоритет
        "Найдено",                                  # Статус
        comment,                                    # Комментарий AI
        ts,                                         # Дата обновления
    ]


def run_self_test() -> int:
    print("[self-test] Пишу тестовую строку в таблицу 'Объявления'...")
    test_item = {"title": "ТЕСТ — эту строку можно удалить",
                 "link": "(нет ссылки, тестовая запись из self_test)"}
    try:
        append_rows([build_sheet_row(test_item, "self-test (проверка записи)")])
    except Exception as exc:  # noqa: BLE001
        print(f"[self-test] ОШИБКА при записи в таблицу: {exc}")
        return 1
    print("[self-test] Успешно записано.")
    return 0


# ----------------------------------------------------------------- замер задержки портала

def update_timing(timing: dict, items: list[dict], source: dict, first_run: bool) -> None:
    """Запоминаем, когда квартира впервые попалась на сайте компании и на портале."""
    now = now_berlin().isoformat()
    portal = source["company"] == "inberlinwohnen"
    field = "portal_first_seen" if portal else "site_first_seen"
    for it in items:
        key = it.get("key")
        if not key:
            continue
        rec = timing.setdefault(key, {"company": it.get("company"), "address": it.get("address")})
        if field not in rec:
            rec[field] = now
            # квартиры, которые уже висели до первого запуска источника, для замера не годятся
            if first_run:
                rec[field + "_baseline"] = True
        if portal and it.get("portal_uploaded") and "portal_uploaded" not in rec:
            rec["portal_uploaded"] = it["portal_uploaded"]


def prune_timing(timing: dict, days: int = 45) -> None:
    cutoff = (now_berlin() - datetime.timedelta(days=days)).isoformat()
    for key in [k for k, v in timing.items()
                if max(v.get("site_first_seen", ""), v.get("portal_first_seen", "")) < cutoff]:
        del timing[key]


# ----------------------------------------------------------------- main

def main() -> int:
    if SELF_TEST_SHEETS:
        return run_self_test()

    config = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))
    sources = config["sources"]
    filters = config.get("filters") or {}
    seen = load_json(SEEN_FILE)
    timing = load_json(TIMING_FILE)
    # общий список уже записанных в таблицу квартир (сайт компании + портал = одна квартира)
    sheet_keys = set(seen.get("_sheet_keys", []))
    total_new = 0

    for source in sources:
        name, stype = source["name"], source["type"]
        checker = CHECKERS.get(stype)
        if checker is None:
            print(f"[warn] Неизвестный тип источника '{stype}' у '{name}', пропускаю")
            continue

        first_run = name not in seen
        seen_ids = set(seen.get(name, []))
        try:
            items = checker(source)
        except Exception as exc:  # noqa: BLE001 — одна упавшая проверка не должна убивать весь прогон
            print(f"[error] Источник '{name}' упал: {exc}")
            continue
        print(f"[info] {name}: получено {len(items)}")

        if stype == "company":
            update_timing(timing, items, source, first_run)

        new_items = []
        for it in items:
            if it["id"] in seen_ids:
                continue
            if stype == "company":
                if it.get("key") in sheet_keys or not passes_filters(it, filters):
                    continue
                if not passes_distance(it, filters):
                    continue
                sheet_keys.add(it.get("key"))
            new_items.append(it)

        if new_items:
            label = name
            rows = []
            for it in new_items:
                if source.get("company") == "inberlinwohnen":
                    label = f"inberlinwohnen → {it.get('company')}"
                rows.append(build_sheet_row(it, label, filters))
            if DRY_RUN:
                for r in rows:
                    print("[dry-run]", r)
            else:
                try:
                    append_rows(rows)
                except Exception as exc:  # noqa: BLE001
                    print(f"[error] Не удалось записать в таблицу для '{name}': {exc}")
                    for it in new_items:
                        sheet_keys.discard(it.get("key"))
                    continue
            total_new += len(new_items)
            for it in new_items:
                print(f"[new] {name}: {it.get('title')} ({it['link']})")

        if USE_TELEGRAM:
            for it in new_items:
                send_telegram(f"🏠 <b>{name}</b>\n{it.get('title')}\n{it['link']}")

        # запоминаем всё, что видели (не только прошедшее фильтр)
        seen[name] = sorted({it["id"] for it in items} | seen_ids)

    seen["_sheet_keys"] = sorted(k for k in sheet_keys if k)
    prune_timing(timing)
    geo.save_cache()
    if not DRY_RUN:
        save_json(SEEN_FILE, seen)
        save_json(TIMING_FILE, timing)
    print(f"Готово. Новых объявлений в таблицу: {total_new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
