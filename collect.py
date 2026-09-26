#!/usr/bin/env python3
"""
Режим collect-only: замер качества СБОРА данных.

Собирает ВСЕ объявления шести компаний без фильтров и для каждого нового
дочитывает подробную страницу. Ничего не пишет ни в Supabase, ни в таблицу —
только в локальные файлы:

  data/collect/state.json            кто когда впервые/последний раз виден (для «только новые»)
  data/collect/listings-<дата>.jsonl по строке на каждое новое объявление: поля + raw
  data/collect/runs-<дата>.jsonl     по строке на компанию за прогон: счётчики, время, запросы, ошибки
  data/collect/events-<дата>.jsonl   появления / исчезновения / смены ссылок и ID

  python collect.py                              один прогон
  python collect.py --loop-minutes 120 --interval 180
"""

import argparse
import datetime as dt
import json
import re
import sys
import time
import traceback
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from parsers import PARSERS
from parsers.common import HTTP, get_detail, page_lines, value_after

OUT = Path(__file__).parent / "data" / "collect"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


# ----------------------------------------------------------------- разбор подробной страницы (общий)

def detail_pairs(html: str) -> dict:
    """Все пары «метка → значение» со страницы: dt/dd, th/td, «Метка:» + следующая строка,
    «Метка: значение» в одной строке. Для оценки, какие поля сайт вообще отдаёт."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        tag.decompose()
    pairs = {}

    def put(k, v):
        k = re.sub(r"\s+", " ", k or "").strip().rstrip(":").strip()
        v = re.sub(r"\s+", " ", v or "").strip()
        if k and v and len(k) <= 40 and len(v) <= 200 and k not in pairs:
            pairs[k] = v

    for dl in soup.find_all("dl"):
        for dt_, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
            put(dt_.get_text(" "), dd.get_text(" "))
    for tr in soup.find_all("tr"):
        th, td = tr.find("th"), tr.find("td")
        if th and td:
            put(th.get_text(" "), td.get_text(" "))
    lines = [ln.strip() for ln in soup.get_text("\n").split("\n") if ln.strip()]
    for i, ln in enumerate(lines):
        m = re.match(r"^([A-ZÄÖÜ][\wäöüß .()/-]{1,38}):\s*(\S.*)$", ln)
        if m:
            put(m.group(1), m.group(2))
        elif ln.endswith(":") and i + 1 < len(lines) and len(ln) <= 41:
            put(ln, lines[i + 1])
    return pairs


FLOOR_KEYS = re.compile(r"^(etage|geschoss|stockwerk|lage im haus)$", re.I)
AVAIL_KEYS = re.compile(r"^(frei ab|bezugsfrei ab|bezugsfrei|verfügbar ab|bezugsfertig ab|bezug ab)$", re.I)


def extra_fields(html: str, pairs: dict, lines_text: str) -> dict:
    out = {}
    lines = page_lines(html)
    floor = value_after(lines, "Etage", "Geschoss", "Stockwerk", "Lage im Haus")
    if floor:
        out["floor"] = floor
    avail = value_after(lines, "frei ab", "Bezugsfrei ab", "Bezugsfrei", "verfügbar ab", "Bezugsfertig ab")
    if avail:
        out["available_from"] = avail
    for k, v in pairs.items():
        if "floor" not in out and FLOOR_KEYS.match(k):
            out["floor"] = v
        if "available_from" not in out and AVAIL_KEYS.match(k):
            out["available_from"] = v
    if "floor" not in out:
        m = re.search(r"\b(\d{1,2})\.\s*(?:Etage|OG|Obergeschoss)\b|\b(Erdgeschoss|Dachgeschoss|Souterrain)\b", lines_text)
        if m:
            out["floor"] = m.group(0)
    if "available_from" not in out:
        m = re.search(r"(?:Frei|Bezugsfrei|Verfügbar) ab:?\s*(\d{2}\.\d{2}\.\d{4}|sofort)", lines_text, re.I)
        if m:
            out["available_from"] = m.group(1)
    m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html)
    if m:
        out["published"] = m.group(1)
    m = re.search(r'"dateModified"\s*:\s*"([^"]+)"', html)
    if m:
        out["modified"] = m.group(1)
    return out


def classify_error(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, requests.Timeout):
        return "таймаут"
    if isinstance(exc, requests.ConnectionError):
        return "нет соединения"
    if isinstance(exc, ValueError) and "JSON" in str(exc):
        return "не JSON (капча/заглушка?)"
    return f"ошибка разбора: {type(exc).__name__}"


# ----------------------------------------------------------------- один прогон

class Collector:
    def __init__(self, out: Path):
        self.out = out
        out.mkdir(parents=True, exist_ok=True)
        self.state_file = out / "state.json"
        self.state = json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file.exists() else {}

    def _append(self, name: str, rec: dict) -> None:
        day = utcnow().strftime("%Y-%m-%d")
        with open(self.out / f"{name}-{day}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def run_company(self, company: str, parser, run_ts: str) -> dict:
        st = self.state.setdefault(company, {"items": {}, "links": {}, "last_run": None})
        rec = {"ts": run_ts, "company": company, "status": "ok", "error": None}
        r0, e0, t0 = HTTP["requests"], HTTP["errors"], time.monotonic()
        try:
            items = parser.fetch()
        except Exception as exc:  # noqa: BLE001
            rec.update(status="error", error=classify_error(exc), trace=traceback.format_exc(limit=2)[-600:],
                       list_sec=round(time.monotonic() - t0, 1), list_requests=HTTP["requests"] - r0,
                       http_errors=HTTP["errors"] - e0)
            print(f"[error] {company}: {rec['error']}", flush=True)
            return rec
        rec.update(list_sec=round(time.monotonic() - t0, 1), list_requests=HTTP["requests"] - r0,
                   meta=dict(getattr(parser, "META", {}) or {}))
        if not items:
            rec["status"] = "empty"
        eids = [x.external_id for x in items]
        links = [x.link for x in items]
        rec.update(items=len(items), unique=len(set(eids)), dup_ids=len(eids) - len(set(eids)),
                   dup_links=len(links) - len(set(links)), site_count=rec["meta"].get("site_count"))

        new, id_changes, link_changes = [], [], []
        seen_now = set()
        r1, t1 = HTTP["requests"], time.monotonic()
        detail_errors = 0
        for x in items:
            if x.external_id in seen_now:
                continue
            seen_now.add(x.external_id)
            old_eid = st["links"].get(x.link)
            if old_eid and old_eid != x.external_id:
                id_changes.append({"link": x.link, "old": old_eid, "new": x.external_id})
            st["links"][x.link] = x.external_id
            item = st["items"].get(x.external_id)
            if item:
                if item.get("gone_at"):          # вернулось после исчезновения
                    self._append("events", {"ts": run_ts, "company": company, "event": "back",
                                            "external_id": x.external_id, "gone_at": item.pop("gone_at")})
                if item["link"] != x.link:
                    link_changes.append({"external_id": x.external_id, "old": item["link"], "new": x.link})
                    item["link"] = x.link
                item["last_seen"] = run_ts
                continue
            # новое объявление: подробная страница для КАЖДОГО (кроме Stadt und Land — всё в API)
            full = {"detail_ok": None}
            if hasattr(parser, "parse_detail"):
                try:
                    html = get_detail(x.link)
                    parser.parse_detail(html, x)
                    pairs = detail_pairs(html)
                    lines_text = " ".join(pairs.values()) + " " + BeautifulSoup(html, "html.parser").get_text(" ")
                    full.update(detail_ok=True, detail_pairs=pairs, **extra_fields(html, pairs, lines_text))
                except Exception as exc:  # noqa: BLE001
                    detail_errors += 1
                    full.update(detail_ok=False, detail_error=classify_error(exc))
            first_run = st["last_run"] is None
            listing = {"ts": run_ts, "company": company, "first_run": first_run, **x.to_dict(), **full}
            self._append("listings", listing)
            st["items"][x.external_id] = {"first_seen": run_ts, "last_seen": run_ts, "link": x.link,
                                          "first_run": first_run, "published": full.get("published")}
            new.append(x.external_id)
            if not first_run:
                self._append("events", {"ts": run_ts, "company": company, "event": "new",
                                        "external_id": x.external_id, "published": full.get("published"),
                                        "address": x.address})
        gone = []
        if st["last_run"]:
            for eid, item in st["items"].items():
                if item.get("last_seen") == st["last_run"] and eid not in seen_now:
                    item["gone_at"] = run_ts
                    gone.append(eid)
                    self._append("events", {"ts": run_ts, "company": company, "event": "gone",
                                            "external_id": eid, "first_seen": item["first_seen"]})
        for ch in id_changes:
            self._append("events", {"ts": run_ts, "company": company, "event": "id_change", **ch})
        for ch in link_changes:
            self._append("events", {"ts": run_ts, "company": company, "event": "link_change", **ch})
        st["last_run"] = run_ts
        rec.update(new=len(new), gone=len(gone), id_changes=len(id_changes), link_changes=len(link_changes),
                   detail_sec=round(time.monotonic() - t1, 1), detail_requests=HTTP["requests"] - r1,
                   detail_errors=detail_errors, http_errors=HTTP["errors"] - e0, ids=sorted(seen_now))
        return rec

    def run(self) -> None:
        run_ts = utcnow().isoformat()
        print(f"===== сбор {run_ts} =====", flush=True)
        for company, parser in PARSERS.items():
            rec = self.run_company(company, parser, run_ts)
            self._append("runs", rec)
            print(f"[сбор] {company}: {rec['status']}, объявлений {rec.get('items')}, на сайте "
                  f"{rec.get('site_count')}, новых {rec.get('new')}, исчезло {rec.get('gone')}, "
                  f"список {rec.get('list_sec')}с/{rec.get('list_requests')} запр., "
                  f"подробные {rec.get('detail_sec')}с/{rec.get('detail_requests')} запр."
                  + (f", ОШИБКА {rec['error']}" if rec.get("error") else ""), flush=True)
            self.state_file.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop-minutes", type=float, default=0)
    ap.add_argument("--interval", type=int, default=180)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    c = Collector(Path(args.out))
    end = time.monotonic() + args.loop_minutes * 60
    while True:
        started = time.monotonic()
        try:
            c.run()
        except Exception as exc:  # noqa: BLE001
            print(f"[error] прогон упал: {exc!r}", flush=True)
        wait = args.interval - (time.monotonic() - started)
        if time.monotonic() + max(wait, 0) >= end:
            break
        time.sleep(max(wait, 5))
    return 0


if __name__ == "__main__":
    sys.exit(main())
