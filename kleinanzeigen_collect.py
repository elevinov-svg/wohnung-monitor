#!/usr/bin/env python3
"""
Сбор Kleinanzeigen (группа C) -> Supabase collected_listings (company = 'Kleinanzeigen').

Только сбор, без оценки «подходит / нет»: WG, Zwischenmiete, расстояние до
Boxhagener Platz, WBS в тексте оценивает фильтр (отдельный этап, общий для всех
источников). В Google-таблицу этот скрипт не пишет.

Запускается ЛОКАЛЬНО (Kleinanzeigen банит IP дата-центров, не запускать в Actions):
Планировщик Windows раз в 30 минут -> scripts/kleinanzeigen_task.ps1 -> этот файл.
Подключается по CDP к уже открытому Chrome (порт 9222) с залогиненной сессией;
сам браузер не запускает и не закрывает — открывает свою вкладку и закрывает её.

Ленты (для каждого района из config.yaml, фильтры — в URL сайта):
  general     — все объявления по цене и комнатам
  wbs_filter  — то же + чекбокс «WBS» сайта
  wbs_text    — то же + полнотекстовый поиск «wbs»
Совпадения между лентами склеиваются по ID объявления (found_via — где нашлось).

Каждая лента листается от новых к старым до первого уже известного ID
(stop_after_known; TOP-объявления закреплены сверху и не считаются). Первый успешный
обход КАЖДОЙ ленты — её «точка отсчёта»: найденное пишется с baseline = true, без
подробных страниц (список таких лент — parser_health.meta.baseline_feeds). Дальше
подробная страница читается только для новых. Ошибка одной ленты / карточки не
останавливает остальные; стоп целиком — только капча.

  Браузер недоступен   -> ничего не делаем, следующая попытка через retry_hours (2 ч).
  Капча / блокировка   -> стоп целиком, уже собранное пишется, пауза block_retry_hours.
  Запуски раньше срока -> тихо выходят (state: data/kleinanzeigen/state.json).

  python kleinanzeigen_collect.py                 обычный запуск (из планировщика)
  python kleinanzeigen_collect.py --now           без случайной задержки и без учёта паузы
  python kleinanzeigen_collect.py --no-db --now   отладка: 1 страница на ленту, 2 подробных, только лог

Код выхода: 0 — норм. (в т.ч. отложено), 1 — ошибка, 3 — капча / блокировка.
"""

import argparse
import datetime as dt
import json
import os
import random
import sys
import threading
import time
import traceback
from dataclasses import dataclass, fields
from pathlib import Path

import requests
import yaml

from models import Listing
from parsers import kleinanzeigen as ka
from parsers.common import StructureError
from storage.supabase import Supabase, SupabaseError

ROOT = Path(__file__).parent
CONFIG = ROOT / "config.yaml"
STATE = ROOT / "data" / "kleinanzeigen" / "state.json"
LOCK = ROOT / "data" / "kleinanzeigen" / "run.lock"
ALERT_AFTER = 3                 # прогонов подряд с ошибкой / блокировкой -> alert
MODEL_COLS = [f.name for f in fields(Listing) if f.name not in ("company", "detail_loaded", "extra")]
NO_DB_DETAILS = 2
WATCHDOG_MIN = 20               # прогон дольше — принудительный выход (Планировщик убивает на 25-й минуте)
# страница готова к разбору: список, пустая выдача или карточка объявления
READY = "#srchrslt-adtable, #srchrslt-content, #viewad-title, #viewad-main"


def log(msg: str) -> None:
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


# ================================================================ состояние (пауза до …)

def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def acquire_lock():
    """Один прогон за раз (задача Планировщика + ручной запуск). Блокировка снимается сама,
    когда процесс завершается, — даже если он упал или был убит."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOCK, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def defer(state: dict, hours: float, why: str) -> None:
    until = utcnow() + dt.timedelta(hours=hours)
    state.update(next_try_after=until.isoformat(), deferred_reason=why)
    save_state(state)
    log(f"[отложено] {why} — следующая попытка не раньше {until.astimezone():%H:%M} ({hours:g} ч)")


# ================================================================ браузер

def cdp_alive(cdp_url: str) -> bool:
    try:
        return requests.get(cdp_url.rstrip("/") + "/json/version", timeout=5).ok
    except requests.RequestException:
        return False


class Browser:
    """Своё окно в уже запущенном Chrome (CDP). Браузер и чужие вкладки не трогаем.

    Отдельное окно в фоне: активная вкладка своего окна не считается «фоновой», и Chrome её
    не замораживает (иначе чтение страницы зависает, пока окно не на экране). Вдобавок Chrome
    запускается с флагами против заморозки (scripts/start_kleinanzeigen_chrome.ps1)."""

    def __init__(self, cdp_url: str):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        try:
            # в профиле много целей CDP (реклама, расширения) — подключение бывает небыстрым; одна повторная попытка
            try:
                self.browser = self.pw.chromium.connect_over_cdp(cdp_url, timeout=60000)
            except Exception:  # noqa: BLE001
                time.sleep(10)
                self.browser = self.pw.chromium.connect_over_cdp(cdp_url, timeout=60000)
            ctx = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
            try:
                cdp = self.browser.new_browser_cdp_session()
                with ctx.expect_page(timeout=10000) as info:
                    cdp.send("Target.createTarget", {"url": "about:blank", "newWindow": True, "background": True})
                self.page = info.value
            except Exception:  # noqa: BLE001 — не вышло с окном: обычная вкладка
                self.page = ctx.new_page()
        except Exception:
            self.pw.stop()
            raise
        self.requests = 0

    def get(self, url: str) -> tuple[str, int | None, str]:
        self.requests += 1
        # Не ждём событий загрузки: в свёрнутом / фоновом окне и при перенаправлении сайта на канонический
        # адрес они приходят поздно или прерываются («interrupted by another navigation»). Достаточно
        # начала ответа, дальше — ждём нужные элементы страницы.
        resp = None
        try:
            resp = self.page.goto(url, wait_until="commit", timeout=45000)
        except Exception as exc:  # noqa: BLE001
            if "interrupted by another navigation" not in str(exc) and "Timeout" not in type(exc).__name__:
                raise
        try:
            self.page.wait_for_selector(READY, state="attached", timeout=40000)
        except Exception:  # noqa: BLE001 — капча / другая вёрстка / не загрузилось: разберёт parse_*
            pass
        self.page.wait_for_timeout(random.randint(800, 2000))      # «прочитать» страницу
        # страница может ещё перенаправляться — тогда content() падает; ждём и повторяем
        for attempt in range(4):
            try:
                return self.page.content(), (resp.status if resp else None), self.page.url
            except Exception as exc:  # noqa: BLE001
                if "navigating" not in str(exc) or attempt == 3:
                    raise
                self.page.wait_for_timeout(1500)

    def close(self) -> None:
        try:
            self.page.close()
        except Exception:  # noqa: BLE001
            pass
        self.pw.stop()      # отключиться; сам Chrome продолжает работать


def pause(span) -> None:
    time.sleep(random.uniform(*span))


# ================================================================ ленты

@dataclass
class Feed:
    name: str           # «friedrichshain:wbs_filter»
    kind: str           # general / wbs_filter / wbs_text
    url: str


def make_feeds(kc: dict) -> list[Feed]:
    rooms = tuple(kc["rooms"]) if kc.get("rooms") else None
    out = []
    for loc in kc["locations"]:
        common = dict(location_slug=loc["slug"], location_id=loc["id"], max_price=kc.get("max_price"), rooms=rooms)
        out.append(Feed(f"{loc['name']}:general", "general", ka.search_url(**common)))
        out.append(Feed(f"{loc['name']}:wbs_filter", "wbs_filter", ka.search_url(**common, wbs_filter=True)))
        if kc.get("wbs_keyword"):
            out.append(Feed(f"{loc['name']}:wbs_text", "wbs_text", ka.search_url(**common, keyword=kc["wbs_keyword"])))
    return out


def scan_feed(browser: Browser, feed: Feed, known: set, found: dict, kc: dict, max_pages: int,
              fresh: bool = True) -> dict:
    """Листает ленту до первого известного ID. Новые — в found (склейка по ID).
    fresh=False — у ленты ещё не было точки отсчёта: её находки — уже висевшие объявления."""
    url, pages, streak, new, stop = feed.url, 0, 0, 0, "конец ленты"
    total = None
    while url:
        if pages >= max_pages:
            stop = f"лимит {max_pages} стр."
            break
        if browser.requests:
            pause(kc["page_pause"])
        html, status, final = browser.get(url)
        page = ka.parse_list(html, final or url, status)
        total = page.total if total is None else total
        pages += 1
        for x in page.items:
            if x.external_id in known:
                if x.extra.get("top"):
                    continue            # TOP закреплён сверху не по дате — не сигнал остановки
                streak += 1
                if streak >= kc.get("stop_after_known", 1):
                    stop = f"известный ID {x.external_id}"
                    return {"pages": pages, "new": new, "total": total, "stop": stop}
                continue
            streak = 0
            cur = found.get(x.external_id)
            if cur is None:
                cur = found[x.external_id] = x
                new += 1
            if feed.name not in cur.found_via:
                cur.found_via.append(feed.name)
            if fresh:
                cur.extra["fresh"] = True      # новое хотя бы в одной ленте с точкой отсчёта
            if feed.kind == "wbs_filter":
                cur.wbs, cur.wbs_source = "да", "фильтр сайта"
        url = page.next_url
    return {"pages": pages, "new": new, "total": total, "stop": stop}


def read_detail(browser: Browser, x: Listing) -> tuple[bool, str | None]:
    try:
        html, status, final = browser.get(x.link)
    except Exception as exc:  # noqa: BLE001 — таймаут одной карточки не должен ронять прогон
        return False, f"загрузка: {type(exc).__name__}: {str(exc)[:150]}"
    if status in (404, 410) or ka.ad_id(final) not in (None, x.external_id) or \
            (status and status < 400 and "/s-anzeige/" not in final):
        return False, f"объявление недоступно (HTTP {status}, {final[:120]})"
    try:
        ka.parse_detail(html, x, status)
        return True, None
    except StructureError as exc:
        return False, f"структура: {exc}"


# ================================================================ база

def load_known(db: Supabase) -> set:
    rows = db.select("collected_listings", {"select": "external_id", "company": f"eq.{ka.COMPANY}"})
    return {r["external_id"] for r in rows}


def load_health(db: Supabase) -> dict:
    rows = db.select("parser_health", {"select": "*", "company": f"eq.{ka.COMPANY}"})
    return rows[0] if rows else {"company": ka.COMPANY, "state": "ok", "fail_streak": 0, "empty_streak": 0, "meta": {}}


def db_row(x: Listing, tsi: str, baseline: bool) -> dict:
    row = {c: getattr(x, c) for c in MODEL_COLS}
    raw = {"list": x.extra.get("list_raw"), "detail": x.extra.get("detail_raw"), "top": x.extra.get("top")}
    row.update(company=ka.COMPANY, raw=raw, first_seen=tsi, last_seen=tsi, baseline=baseline,
               detail_ok=x.extra.get("detail_ok"), detail_error=x.extra.get("detail_error"),
               detail_checked_at=tsi if x.extra.get("detail_ok") else None)
    return row


def update_health(db: Supabase, h: dict, status: str, error: str | None, n_items: int, tsi: str) -> None:
    prev = h.get("state") or "ok"
    if status == "error":
        h["fail_streak"] = (h.get("fail_streak") or 0) + 1
        h["last_error"] = error
        state = "alert" if prev == "alert" or h["fail_streak"] >= ALERT_AFTER else "suspect"
    else:
        h["fail_streak"], h["last_ok_at"], state = 0, tsi, "ok"
    if n_items:
        h["last_count"] = n_items
    if state != prev:
        h["since"] = tsi
        log(f"[ALERT] Kleinanzeigen: {h['fail_streak']} прогонов подряд с ошибкой — {error}" if state == "alert"
            else f"[{state.upper()}] Kleinanzeigen")
    h.update(state=state, updated_at=tsi)
    db.upsert("parser_health", [{k: h.get(k) for k in (
        "company", "state", "fail_streak", "empty_streak", "last_count", "last_ok_at", "since",
        "last_error", "meta", "updated_at")}], "company")


# ================================================================ прогон

def run(args, kc: dict) -> int:
    state = load_state()
    nta = state.get("next_try_after")
    if nta and not args.now and utcnow() < dt.datetime.fromisoformat(nta):
        log(f"[пропуск] пауза до {dt.datetime.fromisoformat(nta).astimezone():%H:%M}: {state.get('deferred_reason')}")
        return 0
    if kc.get("start_jitter") and not args.now:
        time.sleep(random.uniform(0, kc["start_jitter"]))

    if not cdp_alive(kc["cdp_url"]):
        defer(state, kc["retry_hours"], f"браузер недоступен ({kc['cdp_url']})")
        return 0

    db = None if args.no_db else Supabase()
    if db is not None and not db.configured:
        log("[error] SUPABASE_KEY не задан (.envrc.ps1) — или запустите с --no-db")
        return 1
    try:
        known = load_known(db) if db else set()
        health = load_health(db) if db else {"meta": {}}
    except SupabaseError as exc:
        log(f"[db-error] не удалось прочитать базу, прогон пропущен: {exc}")
        return 1
    # ленты, у которых уже есть точка отсчёта (первый успешный обход); у новых лент —
    # всё найденное считается уже висевшим: baseline = true, без подробных страниц
    meta = dict(health.get("meta") or {})
    baseline_feeds = set(meta.get("baseline_feeds") or [])

    # сторожевой таймер: зависший браузер не должен держать прогон до убийства Планировщиком
    watchdog = threading.Timer(WATCHDOG_MIN * 60, lambda: (
        log(f"[error] прогон висит дольше {WATCHDOG_MIN} мин — принудительное завершение"), os._exit(4)))
    watchdog.daemon = True
    watchdog.start()
    try:
        browser = Browser(kc["cdp_url"])
    except Exception as exc:  # noqa: BLE001
        watchdog.cancel()
        defer(state, kc["retry_hours"], f"не удалось подключиться к браузеру: {type(exc).__name__}: {str(exc)[:150]}")
        return 0

    ts = utcnow()
    tsi = ts.isoformat()
    rec = {"company": ka.COMPANY, "started_at": tsi, "status": "ok", "error": None}
    found: dict[str, Listing] = {}
    feeds_meta: dict = {}
    new_baseline: list[str] = []
    blocked: Exception | None = None
    n_det = det_err = 0
    t0 = time.monotonic()
    t1 = None
    try:
        max_pages = 1 if args.no_db else kc["max_pages"]
        for feed in make_feeds(kc):
            fresh = db is None or feed.name in baseline_feeds
            try:
                feeds_meta[feed.name] = scan_feed(browser, feed, known, found, kc, max_pages, fresh=fresh)
                if not fresh:
                    new_baseline.append(feed.name)
                    feeds_meta[feed.name]["baseline"] = True
            except ka.BlockDetected:
                raise
            except Exception as exc:  # noqa: BLE001 — одна лента не должна ронять остальные
                err = f"структура: {exc}" if isinstance(exc, StructureError) else f"{type(exc).__name__}: {str(exc)[:400]}"
                feeds_meta[feed.name] = {"error": err}
                rec["status"], rec["error"] = "error", f"{feed.name}: {err}"
            log(f"  [лента] {feed.name}: {feeds_meta[feed.name]}")
        t1 = time.monotonic()
        fresh_items = [x for x in found.values() if x.extra.get("fresh")]
        if fresh_items:
            limit = NO_DB_DETAILS if args.no_db else (args.max_details if args.max_details is not None else len(fresh_items))
            for x in fresh_items[:limit]:
                pause(kc["detail_pause"])
                ok, err = read_detail(browser, x)
                x.extra.update(detail_ok=ok, detail_error=err)
                n_det += 1
                det_err += not ok
    except ka.BlockDetected as exc:
        blocked = exc
        rec["status"], rec["error"] = "error", f"блокировка: {exc}"[:500]
        log(f"[BLOCK] Kleinanzeigen: {exc} — сканирование остановлено целиком")
    except Exception as exc:  # noqa: BLE001
        rec["status"], rec["error"] = "error", f"{type(exc).__name__}: {str(exc)[:300]}"
        log(f"[error] прогон упал:\n{traceback.format_exc(limit=4)}")
    finally:
        browser.close()
        watchdog.cancel()

    n_fresh = sum(bool(x.extra.get("fresh")) for x in found.values())
    rec.update(finished_at=utcnow().isoformat(), items_count=len(found), new_count=n_fresh,
               detail_new=n_det, detail_errors=det_err, requests=browser.requests,
               list_sec=round((t1 or time.monotonic()) - t0, 1),
               detail_sec=round(time.monotonic() - t1, 1) if t1 else None,
               meta={"feeds": feeds_meta, "baseline_items": len(found) - n_fresh})
    if rec["status"] == "ok" and not found:
        rec["status"] = "empty"
    log(f"[сбор] Kleinanzeigen: {rec['status']}, новых {n_fresh}, точка отсчёта {len(found) - n_fresh} "
        f"(без подробных), подробных {n_det} (ошибок {det_err}), запросов {browser.requests}"
        + (f" — {rec['error']}" if rec["error"] else ""))

    rows = [db_row(x, tsi, baseline=not x.extra.get("fresh")) for x in found.values()]
    if db is None:
        for r in rows[:5]:
            log(f"  [no-db] {r['external_id']} {r['title']!r} kalt={r['kalt']} warm={r['warm']} neben={r['neben']} "
                f"area={r['area']} rooms={r['rooms']} {r['address']!r} продавец={r['seller_type']}/{r['seller_name']!r} "
                f"опубл.={r['published']} via={r['found_via']} wbs={r['wbs']} detail={r['detail_ok']} {r['detail_error'] or ''}")
    else:
        try:
            db.upsert("collected_listings", rows, "company,external_id")
            db.insert("parser_runs", [rec])
            # точка отсчёта ленты фиксируется только после того, как её находки записаны
            health["meta"] = {**meta, "baseline_feeds": sorted(baseline_feeds | set(new_baseline))}
            update_health(db, health, rec["status"], rec["error"], len(found), tsi)
        except SupabaseError as exc:
            log(f"[db-error] запись в базу: {exc}")
            return 1

    if blocked:
        defer(state, kc["block_retry_hours"], f"капча / блокировка: {blocked}")
        return 3
    state.pop("next_try_after", None)
    state.pop("deferred_reason", None)
    state.update(last_run=tsi, last_status=rec["status"], last_new=len(found))
    save_state(state)
    return 1 if rec["status"] == "error" else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", action="store_true", help="без случайной задержки старта и без учёта паузы")
    ap.add_argument("--no-db", action="store_true", help="отладка: 1 страница на ленту, 2 подробных, без записи")
    ap.add_argument("--max-details", type=int, help="не больше стольких подробных страниц за прогон")
    args = ap.parse_args()
    if os.environ.get("GITHUB_ACTIONS"):
        log("[error] Kleinanzeigen запускается только локально (банит IP дата-центров)")
        return 1
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    lock = acquire_lock()
    if lock is None:
        log("[пропуск] другой прогон Kleinanzeigen ещё идёт")
        return 0
    try:
        return run(args, cfg["kleinanzeigen"])
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
