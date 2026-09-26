#!/usr/bin/env python3
"""
Локальный монитор для сайтов с сильным antibot (ImmoScout24, Immowelt,
Kleinanzeigen). В отличие от monitor.py (который живёт в облаке на
GitHub Actions), этот скрипт нужно запускать НА СВОЁМ КОМПЬЮТЕРЕ —
с домашнего IP и через настоящий браузер (Playwright/Chromium), а не
голыми HTTP-запросами. Датацентровые IP (GitHub Actions, AWS и т.п.)
эти сайты банят или заворачивают в капчу почти сразу.

Как это использовать:
  1. Открой ImmoScout24 / Immowelt / Kleinanzeigen в браузере,
     настрой там поиск своими фильтрами (район, цена, комнаты и т.д.)
     как обычно через интерфейс сайта.
  2. Скопируй итоговый URL страницы результатов поиска.
  3. Вставь этот URL в local_sources.yaml (см. пример там).
  4. Запусти этот скрипт (см. README-local.md, как поставить на
     расписание через cron/Task Scheduler).

Почему так, а не строить URL фильтров руками: у каждого сайта своя
непубличная и периодически меняющаяся схема параметров фильтра.
Копирование готового URL из браузера — самый надёжный способ, он
всегда соответствует тому, что сайт реально понимает сегодня.
"""

import datetime
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests
import yaml
from playwright.sync_api import sync_playwright

from sheets import append_rows
from db import insert_listings

BASE_DIR = Path(__file__).parent
SOURCES_FILE = BASE_DIR / "local_sources.yaml"
SEEN_FILE = BASE_DIR / "local_seen.json"

# Telegram пока не используем (см. README.md) — оставлено рабочим на будущее.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
USE_TELEGRAM = os.environ.get("USE_TELEGRAM", "false").lower() == "true"

# Обычный десктопный Chrome, чтобы не выделяться из толпы обычных браузеров.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(
        json.dumps(seen, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[warn] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы — "
              "печатаю в консоль вместо отправки:\n" + text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    if not resp.ok:
        print(f"[error] Telegram API вернул {resp.status_code}: {resp.text}")


def fetch_rendered_html(page, url: str) -> str:
    page.goto(url, wait_until="networkidle", timeout=45000)
    # Небольшая случайная пауза — даём странице "settle" и не долбим
    # сайт мгновенными повторными запросами при следующих итерациях.
    time.sleep(random.uniform(1.5, 3.5))
    return page.content()


def extract_links(html: str, base_url: str, link_pattern: str) -> list[dict]:
    pattern = re.compile(link_pattern)
    # Простой regex-поиск ссылок в HTML — без BeautifulSoup, чтобы не
    # тянуть лишнюю зависимость сюда; для наших целей достаточно.
    hrefs = re.findall(r'href="([^"]+)"', html)
    items = []
    seen_links = set()
    for href in hrefs:
        if not pattern.search(href):
            continue
        full_url = href if href.startswith("http") else requests.compat.urljoin(base_url, href)
        if full_url in seen_links:
            continue
        seen_links.add(full_url)
        items.append({"id": full_url, "title": full_url, "link": full_url})
    return items


def build_sheet_row(item: dict, source_name: str) -> list:
    """Та же схема колонок, что и в monitor.py (вкладка "Объявления")."""
    today = datetime.date.today().isoformat()
    return [
        "", today, source_name, item["link"], "", item.get("title", ""), "", "",
        "", "", "", "", "", "", "", "", "", "", "Найдено",
        "Добавлено автоматически (локальный скрипт), требует проверки", today,
    ]


def main() -> int:
    if not SOURCES_FILE.exists():
        print(f"[error] Не найден {SOURCES_FILE}. Скопируй local_sources.example.yaml "
              f"в local_sources.yaml и заполни своими ссылками.")
        return 1

    sources = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))["sources"]
    seen = load_seen()
    total_new = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, locale="de-DE")
        page = context.new_page()

        for source in sources:
            name = source["name"]
            seen_ids = set(seen.get(name, []))

            try:
                html = fetch_rendered_html(page, source["url"])
                items = extract_links(html, source["url"], source["link_pattern"])
            except Exception as exc:  # noqa: BLE001
                print(f"[error] Источник '{name}' упал: {exc}")
                continue

            new_items = [it for it in items if it["id"] not in seen_ids]

            if new_items:
                rows = [build_sheet_row(it, name) for it in new_items]
                # база Supabase — независимо от таблицы (дубли игнорируются)
                insert_listings([{
                    "source": name, "source_type": "local", "external_id": it["id"],
                    "link": it["link"], "title": it.get("title"), "status": "Найдено",
                    "comment": "Добавлено автоматически (локальный скрипт), требует проверки",
                } for it in new_items])
                try:
                    append_rows(rows)
                except Exception as exc:  # noqa: BLE001
                    print(f"[error] Не удалось записать в таблицу для '{name}': {exc}")
                else:
                    total_new += len(new_items)
                    for it in new_items:
                        print(f"[new] {name}: {it['link']}")

            if USE_TELEGRAM:
                for it in new_items:
                    send_telegram(f"🏠 <b>{name}</b>\n{it['link']}")

            seen[name] = list({it["id"] for it in items} | seen_ids)

            # Пауза между сайтами — не долбим их одновременно/впритык.
            time.sleep(random.uniform(3, 7))

        browser.close()

    save_seen(seen)
    print(f"Готово. Новых объявлений: {total_new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
