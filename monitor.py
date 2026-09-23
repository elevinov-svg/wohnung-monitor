#!/usr/bin/env python3
"""
Wohnung-Monitor: проверяет источники объявлений о квартирах и шлёт
уведомления в Telegram о новых объявлениях.

Запускается периодически (см. .github/workflows/monitor.yml — раз в 30 минут).
Состояние ("что мы уже видели") хранится в seen.json рядом со скриптом.

Как добавить новый источник — см. sources.yaml и комментарии ниже.
"""

import datetime
import json
import os
import re
import sys
from pathlib import Path

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

from sheets import append_rows

BASE_DIR = Path(__file__).parent
SOURCES_FILE = BASE_DIR / "sources.yaml"
SEEN_FILE = BASE_DIR / "seen.json"

# Telegram пока не используем (см. README) — код оставлен рабочим,
# чтобы можно было включить обратно одной строкой, когда понадобится.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
USE_TELEGRAM = os.environ.get("USE_TELEGRAM", "false").lower() == "true"

# Разовая самопроверка: пишет одну тестовую строку в таблицу и выходит,
# не трогая реальный мониторинг. Включается через workflow_dispatch
# input "self_test" на GitHub (см. .github/workflows/monitor.yml).
SELF_TEST_SHEETS = os.environ.get("SELF_TEST_SHEETS", "false").lower() == "true"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


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


def check_rss(source: dict) -> list[dict]:
    """Источник типа 'rss': читает RSS/Atom-фид, возвращает список новых записей."""
    feed = feedparser.parse(source["url"], request_headers=HEADERS)
    items = []
    for entry in feed.entries:
        uid = entry.get("id") or entry.get("link")
        if not uid:
            continue
        items.append({
            "id": uid,
            "title": entry.get("title", "(без заголовка)"),
            "link": entry.get("link", source["url"]),
        })
    return items


def check_html_links(source: dict) -> list[dict]:
    """
    Источник типа 'html_links': скачивает HTML-страницу и вытаскивает
    ссылки, подходящие под link_pattern (обычный regex).

    Это самый простой универсальный вариант для сайтов без RSS
    (WGF, SelbstBau, Gewobag, degewo и т.п.). Он не умеет читать
    JS-рендеринг — если сайт подгружает список через JavaScript,
    этот метод ничего не найдёт, и источник нужно будет доделать
    отдельно (например через playwright).
    """
    resp = requests.get(source["url"], headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    pattern = re.compile(source["link_pattern"])

    items = []
    seen_links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if pattern.search(href):
            full_url = href if href.startswith("http") else requests.compat.urljoin(source["url"], href)
            if full_url in seen_links:
                continue
            seen_links.add(full_url)
            title = a.get_text(strip=True) or full_url
            items.append({"id": full_url, "title": title, "link": full_url})
    return items


CHECKERS = {
    "rss": check_rss,
    "html_links": check_html_links,
}

ROOM_COUNT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[- ]?Zimmer", re.IGNORECASE)


def build_sheet_row(item: dict, source_name: str) -> list:
    """
    Собирает строку под колонки вкладки "Объявления":
    ID, Дата обнаружения, Источник, Ссылка, Район, Адрес, Тип жилья,
    Комнаты, Площадь (м2), Kaltmiete, Nebenkosten, Heizkosten, Warmmiete,
    WBS, Jobcenter, До Ostkreuz, Контакт, Приоритет, Статус,
    Комментарий AI, Дата обновления.

    На этом этапе мы надёжно знаем только источник, ссылку и заголовок —
    остальное (цена, площадь, район, WBS) пока не разбираем из текста
    объявления, это следующий шаг. Пустые поля — заполняются вручную
    или следующей версией скрипта, когда дойдём до парсинга страницы
    самого объявления.
    """
    today = datetime.date.today().isoformat()
    title = item.get("title", "")

    room_match = ROOM_COUNT_RE.search(title)
    rooms = room_match.group(1) if room_match else ""

    return [
        "",                      # ID — не генерируем, чтобы не спорить с нумерацией в таблице
        today,                   # Дата обнаружения
        source_name,             # Источник
        item["link"],            # Ссылка
        "",                      # Район — не определяем автоматически (пока)
        title,                   # Адрес — кладём заголовок целиком, там обычно есть улица
        "",                      # Тип жилья
        rooms,                   # Комнаты — вытащили regex'ом, если формат "N Zimmer" был в заголовке
        "",                      # Площадь (м2)
        "", "", "", "",          # Kaltmiete, Nebenkosten, Heizkosten, Warmmiete
        "",                      # WBS
        "",                      # Jobcenter
        "",                      # До Ostkreuz
        "",                      # Контакт
        "",                      # Приоритет
        "Найдено",               # Статус
        "Добавлено автоматически скриптом мониторинга, требует проверки",  # Комментарий AI
        today,                   # Дата обновления
    ]


def run_self_test() -> int:
    """Пишет одну явно помеченную тестовую строку в таблицу и выходит."""
    print("[self-test] Пишу тестовую строку в таблицу 'Объявления'...")
    test_item = {
        "title": "ТЕСТ — эту строку можно удалить",
        "link": "(нет ссылки, тестовая запись из self_test)",
    }
    try:
        append_rows([build_sheet_row(test_item, "self-test (проверка записи)")])
    except Exception as exc:  # noqa: BLE001
        print(f"[self-test] ОШИБКА при записи в таблицу: {exc}")
        return 1
    print("[self-test] Успешно записано. Проверь таблицу и удали тестовую строку вручную.")
    return 0


def main() -> int:
    if SELF_TEST_SHEETS:
        return run_self_test()

    if not SOURCES_FILE.exists():
        print(f"[error] Не найден {SOURCES_FILE}")
        return 1

    sources = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))["sources"]
    seen = load_seen()
    total_new = 0

    for source in sources:
        name = source["name"]
        stype = source["type"]
        checker = CHECKERS.get(stype)
        if checker is None:
            print(f"[warn] Неизвестный тип источника '{stype}' у '{name}', пропускаю")
            continue

        seen_ids = set(seen.get(name, []))

        try:
            items = checker(source)
        except Exception as exc:  # noqa: BLE001 — одна упавшая проверка не должна убивать весь прогон
            print(f"[error] Источник '{name}' упал: {exc}")
            continue

        new_items = [it for it in items if it["id"] not in seen_ids]

        if new_items:
            rows = [build_sheet_row(it, name) for it in new_items]
            try:
                append_rows(rows)
            except Exception as exc:  # noqa: BLE001
                print(f"[error] Не удалось записать в таблицу для '{name}': {exc}")
            else:
                total_new += len(new_items)
                for it in new_items:
                    print(f"[new] {name}: {it['title']} ({it['link']})")

        if USE_TELEGRAM:
            for it in new_items:
                msg = f"🏠 <b>{name}</b>\n{it['title']}\n{it['link']}"
                send_telegram(msg)

        # запоминаем всё, что видели сейчас (не только новое) —
        # так объявление, которое сняли с публикации и вернули обратно,
        # не будет присылаться повторно при каждом прогоне.
        seen[name] = list({it["id"] for it in items} | seen_ids)

    save_seen(seen)
    print(f"Готово. Новых объявлений: {total_new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
