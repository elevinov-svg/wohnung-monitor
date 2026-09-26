#!/usr/bin/env python3
"""
Точка входа.

  python run.py                          один цикл
  python run.py --loop-minutes 50        цикл ~50 минут, проверка каждые --interval секунд
  python run.py --dry-run                только лог: ничего не пишет ни в таблицу, ни в базу
  python run.py --self-test              тестовая строка в таблицу и в базу

Код выхода 1 — если какая-то компания в этом запуске ПЕРЕШЛА в состояние alert
(workflow становится красным и GitHub присылает письмо), или если self-test не прошёл.
Пока парсер остаётся сломанным, повторных красных запусков нет.
"""

import argparse
import datetime as dt
import os
import sys
import time
from pathlib import Path

import yaml

from geo import Geocoder
from models import Listing
from parsers import PARSERS
from pipeline import BERLIN, Monitor, db_record, sheet_row, utcnow
from storage.sheets import Sheet
from storage.supabase import Supabase, SupabaseError

CONFIG = Path(__file__).parent / "config.yaml"


def log(msg: str) -> None:
    print(msg, flush=True)


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def self_test(cfg: dict, store: Supabase, sheet: Sheet) -> int:
    now = utcnow()
    x = Listing(company="self-test", external_id=now.strftime("%Y%m%d-%H%M%S"),
                link="(тестовая запись self_test — строку можно удалить)",
                title="ТЕСТ — эту строку можно удалить", address="Boxhagener Platz, 10245 Berlin")
    x.extra.update(km=0.0, km_src="коорд.", priority="тест")
    ok = True
    try:
        sheet.append([sheet_row(x, cfg, now.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M"))])
        log(f"[self-test] таблица: записано ({x.sheet_id})")
    except Exception as exc:  # noqa: BLE001
        ok = False
        log(f"[self-test] таблица: ОШИБКА {exc}")
    try:
        store.upsert_listings([db_record(x, cfg, now.isoformat())])
        log(f"[self-test] база: записано (listings, external_id={x.external_id})")
    except Exception as exc:  # noqa: BLE001
        ok = False
        log(f"[self-test] база: ОШИБКА {exc}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=env_flag("DRY_RUN"))
    ap.add_argument("--self-test", action="store_true", default=env_flag("SELF_TEST"))
    ap.add_argument("--loop-minutes", type=float, default=0)
    ap.add_argument("--interval", type=int, default=180)
    ap.add_argument("--only", help="одна компания (для отладки), напр. --only WBM")
    args = ap.parse_args()

    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    store, sheet = Supabase(), Sheet()
    if not store.configured:
        log("[warn] SUPABASE_KEY не задан — состояние не читается и не сохраняется")

    if args.self_test:
        return self_test(cfg, store, sheet)

    cache = {}
    if store.configured:
        try:
            cache = store.load_geocache()
        except SupabaseError as exc:
            log(f"[warn] geocache не прочитан: {exc}")
        if not args.dry_run:
            keep = int(cfg.get("parser_runs_keep_days", 30))
            try:
                store.prune_runs((utcnow() - dt.timedelta(days=keep)).isoformat())
            except SupabaseError as exc:
                log(f"[warn] не удалось почистить parser_runs: {exc}")

    parsers = {k: v for k, v in PARSERS.items() if not args.only or k == args.only}
    mon = Monitor(cfg, parsers, store, sheet, Geocoder(cache), dry_run=args.dry_run, log=log)
    if args.dry_run:
        log("[dry-run] только лог: в таблицу и базу ничего не пишется")

    end = time.monotonic() + args.loop_minutes * 60
    alerts = []
    while True:
        log(f"===== проверка {dt.datetime.now(BERLIN):%Y-%m-%d %H:%M:%S} =====")
        try:
            alerts += mon.run_cycle().alerts
        except Exception as exc:  # noqa: BLE001 — сбой одного цикла не останавливает мониторинг
            log(f"[error] цикл упал: {exc!r}")
        if time.monotonic() + args.interval >= end:
            break
        time.sleep(args.interval)

    if alerts:
        log(f"[ALERT] сломались парсеры: {', '.join(sorted(set(alerts)))} — завершаюсь с ошибкой")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
