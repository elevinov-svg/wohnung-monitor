"""
Один цикл мониторинга:

  парсер компании → отсев уже увиденных → грубый фильтр (тип жилья, Берлин,
  комнаты, цена) → подробная страница (только для прошедших) → фильтр ещё раз →
  геокодинг и расстояние → оценка Jobcenter → запись в Google-таблицу и Supabase.

Все увиденные объявления (и отсеянные тоже) — в seen_listings. Прошедшие фильтр
пишутся в оба хранилища независимо; флаги written_sheet / written_db показывают,
куда уже записано. Недописанное дописывается в следующих циклах, без дублей.
"""

import datetime as dt
import os
import re
import traceback
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from geo import GeoTempError
from models import Listing
from storage.supabase import SupabaseError

BERLIN = ZoneInfo("Europe/Berlin")
SHEET_DATE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S",
                      "%m/%d/%Y %H:%M:%S", "%d.%m.%Y")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def parse_ts(v) -> dt.datetime | None:
    if not v:
        return None
    if isinstance(v, dt.datetime):
        return v
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_num(v) -> str:
    if v is None:
        return ""
    v = float(v)
    return str(int(v)) if v.is_integer() else f"{v:.2f}".replace(".", ",")


def fmt_opt(v) -> str:
    return "—" if v is None else fmt_num(v)


# ================================================================ фильтры

class Filters:
    def __init__(self, cfg: dict):
        f = cfg["filters"]
        self.plz_min, self.plz_max = (str(p) for p in f["berlin_plz"])
        self.max_rooms = f.get("max_rooms")
        self.max_warm = f.get("max_warm")
        self.max_kalt = f.get("max_kalt")
        self.max_km = f.get("max_km")
        self.green_km = f.get("green_km", 2.0)
        self.types = []
        for group in (cfg.get("housing_type_exclude") or {}).values():
            self.types.append((
                group["reason"],
                [re.compile(p, re.I) for p in group.get("patterns") or []],
                [re.compile(p, re.I) for p in group.get("unless") or []],
            ))

    def housing_type(self, x: Listing) -> str | None:
        text = " ".join(filter(None, [x.tags, x.title]))
        for reason, patterns, unless in self.types:
            if any(p.search(text) for p in patterns) and not any(u.search(text) for u in unless):
                return reason
        return None

    def basic(self, x: Listing) -> str | None:
        """Причина отсева или None. Неизвестное поле — не причина."""
        r = self.housing_type(x)
        if r:
            return r
        if x.postcode and re.fullmatch(r"\d{5}", x.postcode):
            if not (self.plz_min <= x.postcode <= self.plz_max):
                return f"не Берлин (индекс {x.postcode})"
        elif (x.district or "").strip().lower() == "brandenburg":
            return "не Берлин (Brandenburg)"
        if x.rooms is not None and self.max_rooms is not None and x.rooms > self.max_rooms:
            return f"комнат {fmt_num(x.rooms)} > {self.max_rooms}"
        if x.warm is not None and self.max_warm is not None:
            if x.warm > self.max_warm:
                return f"Warmmiete {fmt_num(x.warm)} > {self.max_warm}"
        elif x.kalt is not None and self.max_kalt is not None and x.kalt > self.max_kalt:
            return f"Kaltmiete {fmt_num(x.kalt)} > {self.max_kalt} (Warmmiete неизвестна)"
        return None


# ================================================================ оценки для таблицы

def jobcenter_note(x: Listing, cfg: dict) -> str:
    lim = float(cfg["jobcenter"]["bruttokalt"])
    mx = round(lim * (1 + float(cfg["jobcenter"]["surcharge"])))
    kalt, neben = x.kalt, x.neben
    if kalt is not None and neben is not None:
        s = kalt + neben
        if x.heiz_in_neben:
            if kalt > mx:
                return f"выше лимита: одна Kaltmiete {kalt:.0f} > {mx}"
            note = f"Kalt+NK = {s:.0f} (отопление внутри), точный Bruttokalt уточнить"
            if s <= lim:
                note += f"; даже с отоплением ≤ {lim:.0f}"
            return note
        if s <= lim:
            return f"потенциально в лимите (Bruttokalt {s:.0f} ≤ {lim:.0f})"
        if s <= mx:
            return f"только с надбавкой (Bruttokalt {s:.0f}, лимит {lim:.0f} +20% ≈ {mx})"
        return f"выше лимита (Bruttokalt {s:.0f} > {mx})"
    if kalt is not None:
        if kalt > mx:
            return f"выше лимита: одна Kaltmiete {kalt:.0f} > {mx}"
        return f"проверить: Kaltmiete {kalt:.0f}, Nebenkosten неизвестны"
    if x.warm is not None:
        return f"проверить: известна только Warmmiete {x.warm:.0f}"
    return "нужно проверить"


def distance_label(x: Listing) -> str:
    km = x.extra.get("km")
    if km is None:
        return ""
    approx = "~" if x.extra.get("km_src") in ("индекс", "улица") else ""
    return f"{approx}{km:.1f}".replace(".", ",") + " км до Boxhagener Pl."


def comment(x: Listing) -> str:
    parts = [x.title or "", "Добавлено автоматически, требует проверки"]
    if x.extra.get("relisted_from"):
        first = parse_ts(x.extra["relisted_from"])
        parts.append("повторная публикация, впервые видели "
                     + (first.astimezone(BERLIN).strftime("%d.%m.%Y") if first else str(x.extra["relisted_from"])))
    if x.heiz_in_neben:
        parts.append("Nebenkosten включают отопление")
    if x.extra.get("detail_error"):
        parts.append("подробная страница не загрузилась")
    if x.extra.get("km_src") == "индекс":
        parts.append("расстояние оценено по индексу")
    elif x.extra.get("km_src") == "улица":
        parts.append("расстояние по улице (дом не найден на карте)")
    return " | ".join(p for p in parts if p)


def sheet_row(x: Listing, cfg: dict, ts: str) -> list:
    return [
        x.sheet_id,                                   # ID
        ts,                                           # Дата обнаружения (Берлин)
        x.company,                                    # Источник
        x.link,                                       # Ссылка
        x.district or "",                             # Район
        x.address or x.title or "",                   # Адрес
        "Wohnung",                                    # Тип жилья
        fmt_num(x.rooms),                             # Комнаты
        fmt_num(x.area),                              # Площадь (м2)
        fmt_num(x.kalt),                              # Kaltmiete
        fmt_num(x.neben),                             # Nebenkosten
        "в NK" if x.heiz_in_neben else fmt_num(x.heiz),   # Heizkosten
        fmt_num(x.warm),                              # Warmmiete
        x.wbs or "неизвестно",                        # WBS
        jobcenter_note(x, cfg),                       # Jobcenter
        distance_label(x),                            # «До Ostkreuz» — км до Boxhagener Platz
        x.company,                                    # Контакт
        x.extra.get("priority", ""),                  # Приоритет
        "Найдено",                                    # Статус
        comment(x),                                   # Комментарий AI
        ts,                                           # Дата обновления
    ]


def db_record(x: Listing, cfg: dict, now_iso: str) -> dict:
    # status не передаём: при вставке — значение по умолчанию «Найдено»,
    # при повторной записи не затираем то, что поменяли в базе руками
    return {
        "source": x.company, "source_type": "landeseigene", "external_id": x.external_id,
        "listing_key": x.sheet_id, "link": x.link, "title": x.title, "company": x.company,
        "address": x.address, "district": x.district, "postcode": x.postcode, "housing_type": "Wohnung",
        "rooms": x.rooms, "area_m2": x.area, "kalt": x.kalt, "neben": x.neben, "heiz": x.heiz,
        "warm": x.warm, "wbs": x.wbs or "неизвестно", "jobcenter_note": jobcenter_note(x, cfg),
        "distance_km": x.extra.get("km"), "distance_src": x.extra.get("km_src"),
        "priority": x.extra.get("priority") or None, "comment": comment(x), "published_at": x.published,
        "lat": x.lat, "lon": x.lon, "raw": x.to_dict(), "updated_at": now_iso,
    }


# ================================================================ цикл

@dataclass
class Pending:
    """Прошло фильтр, но ещё не записано в одно из хранилищ."""
    x: Listing
    since: dt.datetime            # начало текущей публикации — для проверки дублей в таблице
    need_sheet: bool
    need_db: bool
    sheet_ok: bool = False
    db_ok: bool = False

    @property
    def relisted(self) -> bool:
        return bool(self.x.extra.get("relisted_from"))


@dataclass
class CompanyStats:
    company: str
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    items: int = 0
    new: int = 0
    passed: int = 0
    sheet_written: int = 0
    db_written: int = 0
    error: str | None = None


@dataclass
class CycleResult:
    stats: dict = field(default_factory=dict)
    sheet_written: int = 0
    db_written: int = 0
    errors: int = 0
    alerts: list = field(default_factory=list)      # компании, перешедшие в alert в этом цикле
    recovered: list = field(default_factory=list)


class Monitor:
    def __init__(self, cfg: dict, parsers: dict, store, sheet, geocoder, dry_run: bool = False,
                 log=print, clock=utcnow):
        self.cfg = cfg
        self.filters = Filters(cfg)
        self.parsers = parsers
        self.store = store
        self.sheet = sheet
        self.geo = geocoder
        self.dry_run = dry_run
        self.log = log
        self.clock = clock
        self.health: dict[str, dict] | None = None

    # ------------------------------------------------------------ helpers

    def _store_ok(self) -> bool:
        return self.store is not None and self.store.configured

    def _reject_log(self, x: Listing, reason: str) -> None:
        km = x.extra.get("km")
        if km is None and x.lat is not None and x.lon is not None:
            try:
                km, _ = self.geo.distance(x, allow_temp_error=False)
            except Exception:  # noqa: BLE001
                km = None
        self.log(f"[отсев] {x.company} | {x.address or x.title or x.link} | rooms={fmt_opt(x.rooms)} | "
                 f"warm={fmt_opt(x.warm)} | kalt={fmt_opt(x.kalt)} | "
                 f"{'—' if km is None else f'{km:.1f}'} км | {reason}")

    def evaluate(self, x: Listing, parser) -> tuple[bool | None, str | None]:
        """-> (passed_filter, reject_reason). passed_filter=None — повторить в следующем цикле."""
        reason = self.filters.basic(x)
        if reason:
            return False, reason
        if hasattr(parser, "enrich") and not x.detail_loaded:
            try:
                parser.enrich(x)
                x.extra.pop("detail_error", None)
            except Exception as exc:  # noqa: BLE001 — без подробной страницы всё равно пишем
                x.extra["detail_error"] = str(exc)[:200]
                self.log(f"[warn] {x.company}: подробная страница не загрузилась ({x.link}): {exc}")
            reason = self.filters.basic(x)
            if reason:
                return False, reason
        attempts = int(x.extra.get("geo_attempts") or 0)
        max_attempts = int(self.cfg.get("geo_max_attempts", 10))
        try:
            km, how = self.geo.distance(x, allow_temp_error=attempts + 1 < max_attempts)
        except GeoTempError as exc:
            x.extra["geo_attempts"] = attempts + 1
            self.log(f"[retry] {x.company} | {x.address} | геокодер временно недоступен ({exc}), "
                     f"попытка {attempts + 1} из {max_attempts}")
            return None, f"геокодер временно недоступен: {exc}"
        x.extra.pop("geo_attempts", None)
        x.extra["km"], x.extra["km_src"] = km, how
        if km is None:
            x.extra["priority"] = "🟡 расстояние не определено"
            return True, None
        if self.filters.max_km and km > self.filters.max_km:
            return False, f"далеко: {km:.1f} км > {self.filters.max_km}"
        x.extra["priority"] = "🟢 рядом" if km <= self.filters.green_km else "🟡 в радиусе"
        return True, None

    # ------------------------------------------------------------ компания

    def process_company(self, company: str, parser, result: CycleResult,
                        pending: list[Pending]) -> None:
        now = self.clock()
        st = CompanyStats(company, started_at=now)
        result.stats[company] = st
        try:
            items = parser.fetch()
        except Exception as exc:  # noqa: BLE001 — упавший парсер не останавливает остальные
            st.error = f"{type(exc).__name__}: {exc}"[:500]
            st.finished_at = self.clock()
            self.log(f"[error] {company}: парсер упал: {st.error}")
            self.log(traceback.format_exc(limit=3))
            return
        uniq = {}
        for x in items:
            uniq.setdefault(x.external_id, x)
        items = list(uniq.values())
        st.items = len(items)

        seen, unfinished = {}, {}
        if self._store_ok():
            try:
                seen = self.store.load_seen(company)
                unfinished = {r["external_id"]: r for r in self.store.load_unfinished(company)}
            except SupabaseError as exc:
                self.log(f"[error] {company}: не удалось прочитать состояние из Supabase: {exc} — "
                         f"обрабатываю всё как новое, дубли в таблице отсечёт проверка по ID")
                result.errors += 1
                seen, unfinished = {}, {}
        legacy_by_link = {r["link"]: r for r in seen.values() if r.get("legacy") and r.get("link")}
        relist_after = dt.timedelta(days=int(self.cfg.get("relist_after_days", 14)))
        now_iso = now.isoformat()

        seen_rows, processed = [], set()
        for x in items:
            row = seen.get(x.external_id)
            if row is None and x.link in legacy_by_link:
                # старое объявление из seen.json, которое при переносе удалось опознать только по ссылке
                old = legacy_by_link[x.link]
                seen_rows.append({"company": company, "external_id": x.external_id, "link": x.link,
                                  "first_seen": old["first_seen"], "current_since": old["current_since"],
                                  "last_seen": now_iso, "passed_filter": old["passed_filter"],
                                  "reject_reason": old["reject_reason"], "written_sheet": old["written_sheet"],
                                  "written_db": old["written_db"], "legacy": True})
                continue

            relisted_from = None
            if row is not None:
                last = parse_ts(row.get("last_seen"))
                if last and now - last > relist_after:
                    relisted_from = row.get("first_seen")
                    self.log(f"[relist] {company} | {x.address} | не было с {last:%Y-%m-%d}, считаю новым")
                    row = None

            if row is not None and row.get("passed_filter") is not None:
                # уже решено; дописывание недописанного — ниже, из unfinished
                seen_rows.append({"company": company, "external_id": x.external_id, "link": x.link,
                                  "last_seen": now_iso})
                continue

            if row is None:
                st.new += 1
            else:   # решение не принято в прошлый раз: берём уже прочитанную подробную страницу
                raw = (unfinished.get(x.external_id) or {}).get("raw") or {}
                if raw.get("detail_loaded") and not x.detail_loaded:
                    for k in ("kalt", "neben", "heiz", "warm", "wbs", "postcode", "address", "lat", "lon",
                              "heiz_in_neben", "detail_loaded", "tags"):
                        setattr(x, k, raw.get(k, getattr(x, k)))
                x.extra.update({k: v for k, v in (raw.get("extra") or {}).items()
                                if k in ("geo_attempts", "relisted_from")})
            if relisted_from:
                x.extra["relisted_from"] = relisted_from

            passed, reason = self.evaluate(x, parser)
            processed.add(x.external_id)
            rec = {"company": company, "external_id": x.external_id, "link": x.link, "last_seen": now_iso,
                   "passed_filter": passed, "reject_reason": reason, "raw": x.to_dict()}
            if row is None:   # новое или повторная публикация
                rec.update(current_since=now_iso, written_sheet=False, written_db=False, legacy=False)
                if not relisted_from:
                    rec["first_seen"] = now_iso
            seen_rows.append(rec)
            if passed is False:
                self._reject_log(x, reason)
            elif passed:
                st.passed += 1
                since = now if row is None else (parse_ts(row.get("current_since")) or now)
                pending.append(Pending(x, since, need_sheet=True, need_db=True))
                self.log(f"[new] {company} | {x.address} | rooms={fmt_opt(x.rooms)} | warm={fmt_opt(x.warm)} "
                         f"| kalt={fmt_opt(x.kalt)} | {distance_label(x) or '—'} | {x.extra.get('priority')}")

        # прошли фильтр раньше, но записаны не везде — дописываем
        for eid, r in unfinished.items():
            if eid in processed or not r.get("passed_filter") or not r.get("raw"):
                continue
            x = Listing.from_dict(r["raw"])
            pending.append(Pending(x, parse_ts(r.get("current_since")) or now,
                                   need_sheet=not r.get("written_sheet"), need_db=not r.get("written_db")))
            self.log(f"[retry] {company} | {x.address} | дописываю: "
                     f"{'таблица ' if not r.get('written_sheet') else ''}{'база' if not r.get('written_db') else ''}")

        if seen_rows and self._store_ok() and not self.dry_run:
            try:
                self.store.save_seen(seen_rows)
            except SupabaseError as exc:
                self.log(f"[error] {company}: не удалось сохранить seen_listings: {exc}")
                result.errors += 1
        st.finished_at = self.clock()

    # ------------------------------------------------------------ запись

    def in_sheet(self, p: Pending, existing) -> bool:
        since = p.since.astimezone(BERLIN).replace(tzinfo=None) - dt.timedelta(minutes=1)
        for r in existing:
            if r.id == p.x.sheet_id:
                if not p.relisted:
                    return True
                found = None
                for f in SHEET_DATE_FORMATS:
                    try:
                        found = dt.datetime.strptime(r.found, f)
                        break
                    except ValueError:
                        continue
                if found and found >= since:
                    return True
            elif not p.relisted and not r.id and r.link == p.x.link:
                return True     # старые строки без ID — сверяем по ссылке
        return False

    def write(self, pending: list[Pending], result: CycleResult) -> None:
        now = self.clock()
        ts = now.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M")

        need = [p for p in pending if p.need_sheet]
        if need and self.dry_run:
            for p in need:
                self.log(f"[dry-run] таблица: {sheet_row(p.x, self.cfg, ts)}")
        elif need:
            try:
                existing = self.sheet.existing()
                new = [p for p in need if not self.in_sheet(p, existing)]
                self.sheet.append([sheet_row(p.x, self.cfg, ts) for p in new])
                for p in need:
                    p.sheet_ok = True
                for p in new:
                    result.stats[p.x.company].sheet_written += 1
                result.sheet_written += len(new)
                if len(new) < len(need):
                    self.log(f"[info] таблица: {len(need) - len(new)} уже были там, не дублирую")
            except Exception as exc:  # noqa: BLE001 — ошибка таблицы не мешает базе
                result.errors += 1
                self.log(f"[error] запись в Google-таблицу не удалась: {exc}")

        need = [p for p in pending if p.need_db]
        if need and self.dry_run:
            self.log(f"[dry-run] база: {len(need)} записей в listings")
        elif need:
            try:
                self.store.upsert_listings([db_record(p.x, self.cfg, now.isoformat()) for p in need])
                for p in need:
                    p.db_ok = True
                    result.stats[p.x.company].db_written += 1
                result.db_written += len(need)
            except Exception as exc:  # noqa: BLE001 — ошибка базы не мешает таблице
                result.errors += 1
                self.log(f"[error] запись в Supabase (listings) не удалась: {exc}")

        if self.dry_run or not self._store_ok():
            return
        flags = []
        for p in pending:
            rec = {"company": p.x.company, "external_id": p.x.external_id}
            if p.need_sheet and p.sheet_ok:
                rec["written_sheet"] = True
            if p.need_db and p.db_ok:
                rec["written_db"] = True
            if len(rec) > 2:
                flags.append(rec)
        try:
            self.store.save_seen(flags)
        except SupabaseError as exc:
            result.errors += 1
            self.log(f"[error] не удалось сохранить флаги записи: {exc} — в следующем цикле "
                     f"проверка по ID не даст дублей в таблице, база обновится повторно")

    # ------------------------------------------------------------ здоровье

    def update_health(self, result: CycleResult) -> None:
        if self.health is None:
            self.health = {}
            if self._store_ok():
                try:
                    self.health = self.store.load_health()
                except SupabaseError as exc:
                    self.log(f"[warn] не удалось прочитать parser_health: {exc}")
        limit = int(self.cfg.get("alert_after_failures", 3))
        now_iso = self.clock().isoformat()
        changed = []
        for company, st in result.stats.items():
            h = dict(self.health.get(company) or {"company": company, "state": "ok", "fail_streak": 0})
            failed = st.error is not None or st.items == 0
            h["fail_streak"] = int(h.get("fail_streak") or 0) + 1 if failed else 0
            why = st.error or "0 объявлений"
            if failed:
                h["last_error"] = why
            if failed and h["fail_streak"] >= limit and h.get("state") != "alert":
                h["state"], h["since"] = "alert", now_iso
                result.alerts.append(company)
                self.log(f"[ALERT] {company}: {h['fail_streak']} цикла подряд без объявлений или с ошибкой "
                         f"({why}). Парсер, вероятно, сломан — проверь вёрстку сайта.")
            elif failed and h.get("state") == "alert":
                self.log(f"[alert] {company} всё ещё не работает ({h['fail_streak']} циклов подряд): {why}")
            elif not failed and h.get("state") == "alert":
                h["state"], h["since"] = "ok", now_iso
                result.recovered.append(company)
                self.log(f"[RECOVERED] {company}: парсер снова работает ({st.items} объявлений)")
            h["updated_at"] = now_iso
            if h != self.health.get(company):
                changed.append(h)
            self.health[company] = h
        if changed and self._store_ok() and not self.dry_run:
            try:
                self.store.save_health(changed)
            except SupabaseError as exc:
                self.log(f"[warn] не удалось сохранить parser_health: {exc}")

    # ------------------------------------------------------------ цикл целиком

    def run_cycle(self) -> CycleResult:
        result, pending = CycleResult(), []
        for company, parser in self.parsers.items():
            self.process_company(company, parser, result, pending)
        self.write(pending, result)
        self.update_health(result)

        if self.geo.new_entries and self._store_ok() and not self.dry_run:
            try:
                self.store.save_geocache(self.geo.new_entries)
                self.geo.new_entries = {}
            except SupabaseError as exc:
                self.log(f"[warn] не удалось сохранить geocache: {exc}")

        if self._store_ok() and not self.dry_run:
            gh = os.environ.get("GITHUB_RUN_ID")
            try:
                self.store.insert_runs([{
                    "company": s.company, "started_at": s.started_at.isoformat(),
                    "finished_at": (s.finished_at or self.clock()).isoformat(), "items_count": s.items,
                    "new_count": s.new, "passed_count": s.passed, "sheet_written": s.sheet_written,
                    "db_written": s.db_written, "write_errors": result.errors, "error": s.error,
                    "gh_run_id": gh} for s in result.stats.values()])
            except SupabaseError as exc:
                self.log(f"[warn] не удалось записать parser_runs: {exc}")

        for s in result.stats.values():
            self.log(f"[итог] {s.company}: объявлений {s.items}, новых {s.new}, прошло фильтр {s.passed}, "
                     f"в таблицу {s.sheet_written}, в базу {s.db_written}"
                     + (f", ОШИБКА: {s.error}" if s.error else ""))
        self.log(f"[итог] всего: в таблицу {result.sheet_written}, в базу {result.db_written}, "
                 f"ошибок {result.errors + sum(1 for s in result.stats.values() if s.error)}")
        return result
