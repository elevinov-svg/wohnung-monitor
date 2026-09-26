#!/usr/bin/env python3
"""
Сбор: ВСЕ объявления шести компаний без фильтров -> Supabase.

  collected_listings  все объявления: поля модели, prices, raw, first_seen / last_seen / disappeared_at
  listing_changes     изменения полей (было / стало / когда), исчезновения и возвращения
  parser_runs         строка на компанию за цикл: статус, счётчики, время, повторы, ошибка
  parser_health       состояние парсера: ok / suspect / alert

Каждая компания собирается в своём потоке со своим расписанием — медленная или
зависшая компания не задерживает остальные. Подробная страница читается сразу
для новых объявлений; для известных — раз в сутки (не больше REFRESH_PER_CYCLE
за цикл), чтобы ловить изменения цены.

  python collect.py                               один цикл
  python collect.py --loop-minutes 50 --interval 180
  python collect.py --no-db --only WBM            без базы: только лог (для отладки)

Код выхода 1 — если какая-то компания в этом запуске ПЕРЕШЛА в alert
(прогон красный, GitHub присылает письмо).
"""

import argparse
import datetime as dt
import os
import sys
import threading
import time
import traceback
from dataclasses import fields

import requests

from models import Listing
from parsers import PARSERS
from parsers.common import StructureError, detail_extras, detail_pairs, fill_deposit, get_detail, stats
from storage.supabase import Supabase, SupabaseError

REFRESH_AFTER = dt.timedelta(hours=24)   # известные объявления: подробная страница раз в сутки
REFRESH_PER_CYCLE = 20                   # не больше стольких подробных за цикл (перепроверка и первый цикл)
NO_DB_DETAILS = 3                        # --no-db: подробных на компанию (только для отладки)
ALERT_AFTER = 3                          # циклов подряд с ошибкой / подозрительным «пусто»
PARTIAL_SHARE = 0.9                      # собрано < 90% от счётчика сайта — исчезновения не отмечаем
WBS_FILTERS_EVERY = dt.timedelta(hours=24)   # Gewobag: фильтры WBS без новых ID — раз в сутки

# поля, изменения которых пишутся в listing_changes
TRACKED = ("link", "title", "address", "postcode", "district", "rooms", "area", "kalt", "neben", "heiz",
           "warm", "heiz_in_neben", "deposit_text", "floor", "available_from", "published", "lat", "lon",
           "wbs", "wbs_type", "prices")
MODEL_COLS = [f.name for f in fields(Listing) if f.name not in ("company", "detail_loaded", "extra")]
LOAD_COLS = ["first_seen", "detail_checked_at", "disappeared_at"] + MODEL_COLS
EPOCH = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def classify_error(exc: Exception) -> str:
    if isinstance(exc, StructureError):
        return f"структура: {exc}"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, requests.Timeout):
        return "таймаут"
    if isinstance(exc, requests.ConnectionError):
        return "нет соединения"
    return f"ошибка разбора: {type(exc).__name__}: {str(exc)[:200]}"


def same(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(float(a) - float(b)) < 0.005
    return a == b


class Worker(threading.Thread):
    """Одна компания: свой поток, своё расписание, свой клиент базы."""

    def __init__(self, company: str, parser, args, end: float):
        super().__init__(name=company, daemon=True)
        self.company, self.parser, self.args, self.end = company, parser, args, end
        self.db = None if args.no_db else Supabase()
        self.known: dict[str, dict] | None = None     # external_id -> строка базы (без raw)
        self.health: dict = {}
        self.alerted = False

    # ------------------------------------------------------------ расписание

    def run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                self.cycle()
            except Exception:  # noqa: BLE001 — поток не должен умирать
                log(f"[error] {self.company}: цикл упал:\n{traceback.format_exc(limit=4)}")
            wait = self.args.interval - (time.monotonic() - started)
            if time.monotonic() + max(wait, 0) >= self.end:
                return
            time.sleep(max(wait, 5))

    def load(self) -> None:
        if self.db is None:
            self.known = {}
            self.health = self.health or {"company": self.company, "state": "ok", "fail_streak": 0,
                                          "empty_streak": 0, "meta": {}}
            return
        rows = self.db.select("collected_listings", {"select": ",".join(LOAD_COLS), "company": f"eq.{self.company}"})
        self.known = {r["external_id"]: r for r in rows}
        h = self.db.select("parser_health", {"select": "*", "company": f"eq.{self.company}"})
        self.health = h[0] if h else {"company": self.company, "state": "ok", "fail_streak": 0,
                                      "empty_streak": 0, "meta": {}}

    # ------------------------------------------------------------ один цикл

    def cycle(self) -> None:
        if self.known is None:
            try:
                self.load()
            except SupabaseError as exc:
                log(f"[db-error] {self.company}: не удалось прочитать базу, цикл пропущен: {exc}")
                return
        ts = utcnow()
        st = stats()
        s0 = dict(st)
        t0 = time.monotonic()
        rec = {"gh_run_id": os.environ.get("GITHUB_RUN_ID"), "company": self.company, "started_at": ts.isoformat(),
               "status": "ok", "error": None}
        meta = getattr(self.parser, "META", {})
        items: list[Listing] = []
        try:
            items = self.fetch()
            if meta.get("site_count") and not items:
                raise StructureError(f"сайт пишет {meta['site_count']}, разобрано 0")
        except Exception as exc:  # noqa: BLE001
            rec.update(status="error", error=classify_error(exc))
        rec.update(list_sec=round(time.monotonic() - t0, 1), meta=dict(meta), site_count=meta.get("site_count"),
                   items_count=None if rec["status"] == "error" else len(items))

        mark_gone = True
        if rec["status"] != "error":
            last = self.health.get("last_count") or 0
            if not items:
                # «0 объявлений» при правильной структуре. Если в прошлый раз было больше 0 —
                # подозрительно: исчезновения не отмечаем, перепроверяем в следующем цикле.
                if last > 0 and not self.health.get("empty_streak"):
                    rec["status"], rec["error"] = "suspect", f"0 объявлений, в прошлый раз {last}"
                else:
                    rec["status"] = "empty"
            elif rec["site_count"] and len(items) < rec["site_count"] * PARTIAL_SHARE:
                rec["status"], rec["error"] = "partial", f"собрано {len(items)} из {rec['site_count']}"
                mark_gone = False
            if meta.get("warning"):
                rec["error"] = "; ".join(filter(None, [rec["error"], meta["warning"]]))

        if rec["status"] in ("ok", "empty", "partial"):
            t1 = time.monotonic()
            self.process(items, ts, rec, mark_gone)
            rec["detail_sec"] = round(time.monotonic() - t1, 1)

        rec.update(finished_at=utcnow().isoformat(), requests=st["requests"] - s0["requests"],
                   http_errors=st["errors"] - s0["errors"], retries=st["retries"] - s0["retries"])
        self.update_health(rec, len(items), ts)
        self.write_run(rec)
        log(f"[сбор] {self.company}: {rec['status']}, объявлений {rec.get('items_count')}"
            f"{' / сайт ' + str(rec['site_count']) if rec.get('site_count') else ''}, "
            f"новых {rec.get('new_count', 0)}, исчезло {rec.get('gone_count', 0)}, вернулось {rec.get('back_count', 0)}, "
            f"изменилось {rec.get('changed_count', 0)}, подробных {rec.get('detail_new', 0)}+{rec.get('detail_refresh', 0)}, "
            f"{rec['list_sec']}с/{rec['requests']} запр., повторов {rec['retries']}"
            + (f" — {rec['error']}" if rec.get("error") else ""))

    def fetch(self) -> list[Listing]:
        if self.company == "Gewobag":
            last = parse_ts((self.health.get("meta") or {}).get("wbs_filters_at"))
            active = {k for k, r in self.known.items() if not r.get("disappeared_at")}
            items = self.parser.fetch(known=active if active else None,
                                      force_wbs=last is None or utcnow() - last > WBS_FILTERS_EVERY)
            if self.parser.META.get("wbs_filters") == "обойдены":
                self.health["meta"] = {**(self.health.get("meta") or {}), "wbs_filters_at": utcnow().isoformat()}
        else:
            items = self.parser.fetch()
        uniq = {}
        for x in items:
            uniq.setdefault(x.external_id, x)
        return list(uniq.values())

    # ------------------------------------------------------------ обработка списка

    def read_detail(self, x: Listing) -> tuple[bool, dict]:
        """Подробная страница в объявление: (удалось ли, часть raw)."""
        try:
            html = get_detail(x.link)
            self.parser.parse_detail(html, x)
            pairs = detail_pairs(html)
            detail_extras(html, x, pairs)
            return True, {"detail_pairs": pairs}
        except Exception as exc:  # noqa: BLE001
            return False, {"detail_error": classify_error(exc)}

    def process(self, items: list[Listing], ts: dt.datetime, rec: dict, mark_gone: bool) -> None:
        tsi = ts.isoformat()
        baseline = not self.known          # первый цикл компании: first_seen — не реальное появление
        has_detail = hasattr(self.parser, "parse_detail")
        limit = NO_DB_DETAILS if self.db is None else REFRESH_PER_CYCLE
        # известные: подробная раз в сутки (и те, у кого её ещё не было), самые давние первыми
        due = sorted((x for x in items if has_detail and x.external_id in self.known
                      and (parse_ts(self.known[x.external_id].get("detail_checked_at")) or EPOCH) < ts - REFRESH_AFTER),
                     key=lambda x: self.known[x.external_id].get("detail_checked_at") or "")
        refresh = {x.external_id for x in due[:limit]}

        rows, changes = [], []
        n_new = n_det_new = n_det_ref = det_err = n_changed = 0
        for x in items:
            old = self.known.get(x.external_id)
            if old and x.extra.get("wbs_filters_skipped") and (old.get("wbs_source") == "фильтр сайта" or x.wbs is None):
                # Gewobag без обхода фильтров: WBS — из базы (фильтр сильнее заголовка)
                x.wbs, x.wbs_source, x.wbs_type = old.get("wbs"), old.get("wbs_source"), old.get("wbs_type") or x.wbs_type
            raw = {"list": x.extra.get("list_raw"),
                   "flags": {k: v for k, v in x.extra.items() if k != "list_raw"} or None}
            if old is None:
                # новые — сразу; в первом цикле (baseline) — первые limit, остальные в следующих циклах
                want = has_detail and (not baseline or n_det_new < limit)
                if self.db is None:
                    want = has_detail and n_det_new < limit
            else:
                want = x.external_id in refresh
            detail = None
            if want:
                detail, part = self.read_detail(x)
                raw.update(part)
                det_err += not detail
                n_det_new += old is None
                n_det_ref += old is not None
            fill_deposit(x)
            row = {c: getattr(x, c) for c in MODEL_COLS}
            det = {"detail_ok": detail, "detail_error": raw.get("detail_error")}
            if detail:
                det["detail_checked_at"] = tsi

            if old is None:
                n_new += 1
                rows.append({**row, **det, "company": self.company, "raw": raw, "first_seen": tsi,
                             "last_seen": tsi, "baseline": baseline})
                continue

            # известное объявление
            first_fill = not old.get("detail_checked_at")   # первое чтение подробной — заполнение, не изменение
            diff = []
            for c in TRACKED:
                a, b = old.get(c), row.get(c)
                if not detail:
                    # только список: детальных полей в нём нет — пустое значит «не знаем», а не «убрали»
                    if b is None:
                        continue
                    if c == "prices":
                        b = row[c] = {**(a or {}), **b}
                if same(a, b):
                    continue
                diff.append(c)
                if not (detail and first_fill):
                    changes.append({"company": self.company, "external_id": x.external_id, "changed_at": tsi,
                                    "field": c, "old_value": a, "new_value": b,
                                    "source": "подробная" if detail else "список"})
            if diff and not (detail and first_fill):
                n_changed += 1
            if detail:
                upd = {**row, **det, "raw": raw}                   # полные данные — переписываем всё
            elif diff or detail is False:
                upd = {c: row[c] for c in diff}                     # только изменившееся
                if "wbs" in diff or "wbs_type" in diff:
                    upd["wbs_source"] = row["wbs_source"]
                if detail is False:
                    upd.update(det)
            else:
                continue
            rows.append({**upd, "company": self.company, "external_id": x.external_id, "link": row["link"],
                         "first_seen": old["first_seen"], "last_seen": tsi})

        rec.update(new_count=n_new, detail_new=n_det_new, detail_refresh=n_det_ref, detail_errors=det_err,
                   changed_count=n_changed)
        if self.db is None:
            for r in rows[:NO_DB_DETAILS]:
                log(f"  [no-db] {self.company} {r['external_id']}: warm={r.get('warm')} kalt={r.get('kalt')} "
                    f"neben={r.get('neben')} heiz={r.get('heiz')} wbs={r.get('wbs')}/{r.get('wbs_source')}/"
                    f"{r.get('wbs_type')} этаж={r.get('floor')} с={r.get('available_from')} "
                    f"Kaution={r.get('deposit')} «{r.get('deposit_text')}» опубл.={r.get('published')} "
                    f"коорд.={r.get('lat')},{r.get('lon')} detail={r.get('detail_ok')} {r.get('detail_error') or ''}")
            return
        try:
            self.db.upsert("collected_listings", rows, "company,external_id")
            events = self.db.rpc("collect_mark_seen", {"p_company": self.company,
                                                       "p_ids": [x.external_id for x in items],
                                                       "p_ts": tsi, "p_mark_gone": mark_gone})
            self.db.insert("listing_changes", changes)
        except SupabaseError as exc:
            rec["status"], rec["error"] = "error", f"запись в базу: {exc}"[:500]
            self.known = None      # перечитать базу в следующем цикле
            return
        rec["gone_count"] = sum(e["event"] == "gone" for e in events)
        rec["back_count"] = sum(e["event"] == "back" for e in events)
        # память = база, без повторного чтения
        for r in rows:
            k = self.known.setdefault(r["external_id"], {})
            k.update({c: v for c, v in r.items() if c in LOAD_COLS or c == "external_id"})
        gone = {e["external_id"] for e in events if e["event"] == "gone"}
        for eid, k in self.known.items():
            if eid in gone:
                k["disappeared_at"] = tsi
        for x in items:
            self.known[x.external_id]["disappeared_at"] = None

    # ------------------------------------------------------------ здоровье и журнал

    def update_health(self, rec: dict, n_items: int, ts: dt.datetime) -> None:
        h, tsi, status = self.health, ts.isoformat(), rec["status"]
        prev = h.get("state") or "ok"
        if status in ("error", "suspect"):
            h["fail_streak"] = (h.get("fail_streak") or 0) + 1
            h["last_error"] = rec["error"]
            state = "alert" if prev == "alert" or h["fail_streak"] >= ALERT_AFTER else "suspect"
        else:
            h["fail_streak"] = 0
            h["last_ok_at"] = tsi
            state = "ok"
        if status in ("empty", "suspect"):
            h["empty_streak"] = (h.get("empty_streak") or 0) + 1
        elif status != "error":
            h["empty_streak"] = 0
        if n_items:
            h["last_count"] = n_items
        if state != prev:
            h["since"] = tsi
            if state == "alert":
                self.alerted = True
                log(f"[ALERT] {self.company}: {h['fail_streak']} циклов подряд без данных — {rec['error']}")
            elif prev == "alert":
                log(f"[RECOVERED] {self.company}: снова работает")
        h.update(state=state, company=self.company, updated_at=tsi)
        if self.db is None:
            return
        try:
            self.db.upsert("parser_health", [{k: h.get(k) for k in (
                "company", "state", "fail_streak", "empty_streak", "last_count", "last_ok_at", "since",
                "last_error", "meta", "updated_at")}], "company")
        except SupabaseError as exc:
            log(f"[db-error] {self.company}: parser_health: {exc}")

    def write_run(self, rec: dict) -> None:
        if self.db is None:
            return
        try:
            self.db.insert("parser_runs", [rec])
        except SupabaseError as exc:
            log(f"[db-error] {self.company}: parser_runs: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop-minutes", type=float, default=0)
    ap.add_argument("--interval", type=int, default=180)
    ap.add_argument("--only", help="одна компания, напр. --only WBM")
    ap.add_argument("--no-db", action="store_true", help="без базы: только лог, подробных не больше 3")
    args = ap.parse_args()
    if not args.no_db and not Supabase().configured:
        log("[error] SUPABASE_KEY не задан (или запустите с --no-db)")
        return 2
    end = time.monotonic() + args.loop_minutes * 60
    log(f"===== сбор {utcnow().isoformat()}: {args.loop_minutes} мин, проверка каждые {args.interval} с =====")
    workers = [Worker(c, p, args, end) for c, p in PARSERS.items() if not args.only or c == args.only]
    for w in workers:
        w.start()
    for w in workers:
        # поток сам завершается после последнего цикла; запас — на цикл, идущий в момент end
        w.join(timeout=max(end - time.monotonic(), 0) + 240)
        if w.is_alive():
            log(f"[error] {w.company}: цикл не завершился вовремя, прогон заканчивается без него")
    return 1 if any(w.alerted for w in workers) else 0


if __name__ == "__main__":
    sys.exit(main())
