#!/usr/bin/env python3
"""
Разовый перенос старого состояния (seen.json, geocache.json из истории git)
в Supabase: seen_listings и geocache.

  python scripts/import_seen_json.py --seen OLD_seen.json --geocache OLD_geocache.json \
      --out supabase/seed_legacy.sql --report legacy_report.md [--restore 'HOWOGE:1770-…,WBM:…']

Что делает:
  * ID объекта берётся из старой ссылки (HOWOGE, Gewobag, GESOBAU, Stadt und Land);
    у degewo и WBM в ссылке slug — их сопоставляем с живым списком по ссылке,
    остальные сохраняем как 'legacy:<ссылка>' (в pipeline их ловит сверка по ссылке);
  * всё, что сейчас висит на сайтах, прогоняется через новый фильтр. Прошедшие
    фильтр в SQL НЕ попадают без решения — идут в отчёт (--report). Те, что
    перечислены в --restore, тоже не попадают в SQL — pipeline запишет их как новые;
    остальные прошедшие после решения добавляются с written_sheet/written_db = true;
  * geocache.json -> таблица geocache.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from geo import Geocoder, clean_address  # noqa: E402
from parsers import PARSERS  # noqa: E402
from pipeline import Monitor, distance_label, fmt_opt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

ID_FROM_LINK = {
    "HOWOGE": re.compile(r"/detail/([\w-]+)\.html"),
    "Gewobag": re.compile(r"/mietangebote/(\d{4}-\d{5}-\d{4}-\d{4})"),
    "GESOBAU": re.compile(r"(\d{2}-\d{5}-\d{5}-\d{4})-[0-9a-f]{8}-"),
    "Stadt und Land": re.compile(r"/wohnungssuche/([^/?#]+)$"),
}


def eid_from_link(company: str, link: str) -> str | None:
    rx = ID_FROM_LINK.get(company)
    m = rx.search(link) if rx else None
    if not m:
        return None
    v = m.group(1)
    if company == "Stadt und Land":
        v = v.replace("%2F", "/").replace("%2f", "/")
    return v.upper()


def sql_str(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


class NoStore:
    configured = False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seen", required=True)
    ap.add_argument("--geocache")
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--restore", default="", help="через запятую: ID вида 'HOWOGE:1770-24035-55' — вернуть в таблицу")
    ap.add_argument("--decided", action="store_true",
                    help="решение принято: прошедшие фильтр и НЕ указанные в --restore пишутся как уже записанные")
    args = ap.parse_args()

    seen = json.loads(Path(args.seen).read_text(encoding="utf-8"))
    restore = {s.strip() for s in args.restore.split(",") if s.strip()}
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

    cache = {}
    if args.geocache:
        for q, c in json.loads(Path(args.geocache).read_text(encoding="utf-8")).items():
            cache[clean_address(q)] = tuple(c) if c else None
    geo = Geocoder(cache)
    mon = Monitor(cfg, PARSERS, NoStore(), None, geo, dry_run=True, log=lambda m: None)

    rows, report = [], []
    for company, parser in PARSERS.items():
        links = set(seen.get(company) or [])
        live = parser.fetch()
        live_by_link = {x.link: x for x in live}
        live_by_eid = {x.external_id: x for x in live}
        n_eid = n_live = n_legacy = 0
        for link in sorted(links):
            x = live_by_link.get(link)
            eid = eid_from_link(company, link) or (x.external_id if x else None)
            if eid and eid in live_by_eid:
                x = live_by_eid[eid]
            if not eid:
                eid, n_legacy = f"legacy:{link}", n_legacy + 1
            else:
                n_eid += 1
            row = {"company": company, "external_id": eid, "link": link, "legacy": True,
                   "passed_filter": False, "reject_reason": "перенесено из seen.json",
                   "written_sheet": True, "written_db": True}
            if x is not None:
                n_live += 1
                passed, reason = mon.evaluate(x, parser)
                row.update(passed_filter=bool(passed), reject_reason=reason or "перенесено из seen.json",
                           raw=x.to_dict(), link=x.link)
                if passed:
                    report.append((x, x.sheet_id in restore))
                    if not args.decided or x.sheet_id in restore:
                        continue    # без решения в SQL не пишем; «вернуть» — запишет pipeline
            rows.append(row)
        print(f"{company}: в seen.json {len(links)}, ID из ссылки/живого списка {n_eid}, "
              f"legacy-ссылок {n_legacy}, сейчас на сайте {n_live}")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("-- Перенос seen.json -> seen_listings (сгенерировано scripts/import_seen_json.py)\n")
        for r in rows:
            f.write("insert into public.seen_listings (company, external_id, link, legacy, passed_filter, "
                    "reject_reason, written_sheet, written_db, raw) values ("
                    + ", ".join(sql_str(r.get(k)) for k in ("company", "external_id", "link", "legacy",
                                                             "passed_filter", "reject_reason",
                                                             "written_sheet", "written_db"))
                    + ", " + (sql_str(json.dumps(r["raw"], ensure_ascii=False)) + "::jsonb" if r.get("raw") else "null")
                    + ") on conflict (company, external_id) do nothing;\n")
        for q, c in geo.cache.items():
            f.write(f"insert into public.geocache (query, lat, lon) values ({sql_str(q)}, "
                    f"{sql_str(c[0] if c else None)}, {sql_str(c[1] if c else None)}) on conflict (query) do nothing;\n")

    with open(args.report, "w", encoding="utf-8") as f:
        f.write("| ID | Адрес | Комн. | Warm | Kalt | км | Приоритет | Ссылка |\n|---|---|---|---|---|---|---|---|\n")
        for x, _ in report:
            f.write(f"| {x.sheet_id} | {x.address} | {fmt_opt(x.rooms)} | {fmt_opt(x.warm)} | {fmt_opt(x.kalt)} | "
                    f"{distance_label(x) or '—'} | {x.extra.get('priority')} | {x.link} |\n")
    print(f"SQL: {args.out} ({len(rows)} строк seen_listings, {len(geo.cache)} geocache)")
    print(f"Легаси, прошедшие новый фильтр и висящие на сайтах: {len(report)} -> {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
