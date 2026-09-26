"""Логика pipeline на подставных хранилищах: фильтры, запись в оба места,
дописывание после сбоя, повторная публикация, alert."""

import datetime as dt
from pathlib import Path

import pytest
import yaml

from geo import GeoTempError
from models import Listing
from pipeline import Filters, Monitor, jobcenter_note
from storage.sheets import SheetRow
from storage.supabase import SupabaseError

CFG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"))
T0 = dt.datetime(2026, 10, 1, 10, 0, tzinfo=dt.timezone.utc)


def L(eid="1", **kw) -> Listing:
    base = dict(company="TestCo", external_id=eid, link=f"https://example.org/{eid}",
                address="Boxhagener Str. 1, 10245 Berlin", postcode="10245", rooms=2, warm=600,
                detail_loaded=True)
    base.update(kw)
    return Listing(**base)


# ================================================================ подставные объекты

class FakeStore:
    configured = True

    def __init__(self):
        self.seen, self.listings, self.runs, self.health, self.geocache = {}, {}, [], {}, {}
        self.fail_listings = False

    def load_seen(self, company):
        return {k[1]: dict(v) for k, v in self.seen.items() if k[0] == company}

    def load_unfinished(self, company):
        return [dict(v) for k, v in self.seen.items() if k[0] == company and (
            v.get("passed_filter") is None or (v.get("passed_filter") and
                                               not (v.get("written_sheet") and v.get("written_db"))))]

    def save_seen(self, rows):
        for r in rows:
            key = (r["company"], r["external_id"])
            cur = self.seen.setdefault(key, {"first_seen": r.get("last_seen"), "current_since": r.get("last_seen"),
                                             "passed_filter": None, "written_sheet": False, "written_db": False,
                                             "legacy": False})
            cur.update(r)

    def upsert_listings(self, rows):
        if self.fail_listings:
            raise SupabaseError("listings: HTTP 503")
        for r in rows:
            self.listings[(r["source"], r["external_id"])] = r

    def insert_runs(self, rows):
        self.runs += rows

    def load_health(self):
        return dict(self.health)

    def save_health(self, rows):
        for r in rows:
            self.health[r["company"]] = dict(r)

    def save_geocache(self, entries):
        self.geocache.update(entries)


class FakeSheet:
    def __init__(self):
        self.rows, self.fail = [], False

    def existing(self):
        if self.fail:
            raise RuntimeError("Sheets API 500")
        return [SheetRow(r[0], r[1], r[3]) for r in self.rows]

    def append(self, rows):
        if self.fail:
            raise RuntimeError("Sheets API 500")
        self.rows += rows


class FakeGeo:
    def __init__(self, km=1.0):
        self.km, self.fail, self.new_entries = km, False, {}

    def distance(self, x, allow_temp_error=True):
        if self.fail and allow_temp_error:
            raise GeoTempError("HTTP 429")
        if self.fail:
            return None, None
        return self.km, "адрес"


class FakeParser:
    COMPANY = "TestCo"

    def __init__(self, items):
        self.items, self.fail = items, False

    def fetch(self):
        if self.fail:
            raise RuntimeError("вёрстка поменялась")
        return [Listing.from_dict(i.to_dict()) for i in self.items]


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def make(items, **kw):
    store, sheet, geo, parser, clock = FakeStore(), FakeSheet(), FakeGeo(), FakeParser(items), Clock()
    logs = []
    mon = Monitor(CFG, {"TestCo": parser}, store, sheet, geo, log=logs.append, clock=clock, **kw)
    return mon, store, sheet, geo, parser, clock, logs


# ================================================================ фильтры

F = Filters(CFG)


@pytest.mark.parametrize("x,reason", [
    (L(rooms=None, warm=None, kalt=None), None),                    # всё неизвестно — не отсеиваем
    (L(postcode=None, district=None), None),
    (L(rooms=3), "комнат 3 > 2"),
    (L(warm=811), "Warmmiete 811 > 750"),
    (L(warm=None, kalt=650), "Kaltmiete 650 > 600 (Warmmiete неизвестна)"),
    (L(warm=700, kalt=650), None),                                  # Kalt-порог только без Warm
    (L(postcode="16341"), "не Берлин (индекс 16341)"),
    (L(postcode=None, district="Brandenburg"), "не Берлин (Brandenburg)"),
])
def test_basic_filter(x, reason):
    assert F.basic(x) == reason


@pytest.mark.parametrize("title,reason", [
    ("Möbliertes Studenten WG-Zimmer - only for students!", "студенческое жильё"),
    ("Möbliertes Studentenzimmer - Bornsdorfer Str. 37B, Zi.2", "студенческое жильё"),
    ("1-Zimmer-Wohnungen mit WBS bis 140 - ideal für Studierende und Alleinstehende", None),
    ("Neubau mit Fußbodenheizung im Erstbezug im Gartenfeld / WG-tauglich", None),
    ("Rentnerwohnung nur mit WBS", "жильё для пожилых"),
    ("Seniorenresidenz Alt-Britz / Barrierefreie 1 Zimmer Wohnung, erst ab 60 Jahren", "жильё для пожилых"),
    ("WohnAktiv! ab 60 Jahren", "жильё для пожилых"),
    ("Seniorenwohnung 60+", "жильё для пожилых"),
    ("Nettes Wohnen mit der älteren Generation", "жильё для пожилых"),
    ("Frei ab 01.11. — 2 Zimmer mit Balkon", None),
    ("Wohnung ab 60 m² mit Balkon", None),
    ("Tiefgaragenstellplatz", "не жильё (Gewerbe/Stellplatz/Garage/Lager)"),
    ("Gewerbeeinheit im Erdgeschoss", "не жильё (Gewerbe/Stellplatz/Garage/Lager)"),
    ("2-Zimmer-Wohnung mit Tiefgaragenstellplatz", None),
    ("Schicke Neubauwohnung / WBS 220/180/160 erforderlich", None),   # WBS — не причина отсева
])
def test_housing_type(title, reason):
    assert F.housing_type(L(title=title, tags=title)) == reason


def test_jobcenter_notes():
    assert jobcenter_note(L(kalt=327.77, neben=91.51, heiz=67.73), CFG).startswith("потенциально в лимите")
    assert jobcenter_note(L(kalt=381.74, neben=73), CFG).startswith("только с надбавкой")
    assert jobcenter_note(L(kalt=500, neben=120), CFG).startswith("выше лимита")
    hot = jobcenter_note(L(kalt=436.46, neben=198.34, heiz_in_neben=True), CFG)
    assert hot == "Kalt+NK = 635 (отопление внутри), точный Bruttokalt уточнить"
    assert "даже с отоплением" in jobcenter_note(L(kalt=280, neben=140, heiz_in_neben=True), CFG)
    assert jobcenter_note(L(kalt=560, neben=100, heiz_in_neben=True), CFG).startswith("выше лимита: одна Kaltmiete")
    assert jobcenter_note(L(kalt=None, neben=None, warm=595), CFG).startswith("проверить: известна только Warmmiete")


# ================================================================ цикл и запись

def test_new_listing_written_to_both_and_not_duplicated():
    mon, store, sheet, *_ = make([L("A"), L("B", warm=900)])
    r = mon.run_cycle()
    assert (r.sheet_written, r.db_written) == (1, 1)
    assert sheet.rows[0][0] == "TestCo:A" and ("TestCo", "A") in store.listings
    assert store.seen[("TestCo", "A")]["written_sheet"] and store.seen[("TestCo", "A")]["written_db"]
    rej = store.seen[("TestCo", "B")]
    assert rej["passed_filter"] is False and rej["reject_reason"] == "Warmmiete 900 > 750"
    assert ("TestCo", "B") not in store.listings
    r = mon.run_cycle()
    assert (r.sheet_written, r.db_written) == (0, 0) and len(sheet.rows) == 1


def test_reject_is_logged_with_reason():
    mon, *_, logs = make([L("B", warm=900)])
    mon.run_cycle()
    line = next(m for m in logs if m.startswith("[отсев]"))
    assert "TestCo" in line and "warm=900" in line and "Warmmiete 900 > 750" in line


def test_sheet_failure_does_not_block_db_and_is_retried():
    mon, store, sheet, *_ = make([L("A")])
    sheet.fail = True
    r = mon.run_cycle()
    assert (r.sheet_written, r.db_written, r.errors) == (0, 1, 1)
    assert not store.seen[("TestCo", "A")]["written_sheet"] and store.seen[("TestCo", "A")]["written_db"]
    sheet.fail = False
    r = mon.run_cycle()
    assert (r.sheet_written, r.db_written) == (1, 0)        # дописали только в таблицу
    assert len(sheet.rows) == 1 and store.seen[("TestCo", "A")]["written_sheet"]


def test_db_failure_does_not_block_sheet_and_is_retried():
    mon, store, sheet, *_ = make([L("A")])
    store.fail_listings = True
    r = mon.run_cycle()
    assert (r.sheet_written, r.db_written) == (1, 0)
    store.fail_listings = False
    r = mon.run_cycle()
    assert (r.sheet_written, r.db_written) == (0, 1) and len(sheet.rows) == 1


def test_lost_flag_does_not_duplicate_sheet_row():
    """Строка в таблицу записалась, а флаг сохранить не удалось — повторной строки нет."""
    mon, store, sheet, *_ = make([L("A")])
    mon.run_cycle()
    store.seen[("TestCo", "A")]["written_sheet"] = False
    mon.run_cycle()
    assert len(sheet.rows) == 1 and store.seen[("TestCo", "A")]["written_sheet"]


def test_old_sheet_row_without_id_matched_by_link():
    mon, store, sheet, *_ = make([L("A")])
    sheet.rows.append(["", "25.09.2026 17:27:00", "TestCo", "https://example.org/A"] + [""] * 17)
    r = mon.run_cycle()
    assert r.sheet_written == 0 and len(sheet.rows) == 1 and store.seen[("TestCo", "A")]["written_sheet"]


def test_geocoder_temp_error_is_retried_not_seen():
    mon, store, sheet, geo, *_ = make([L("A")])
    geo.fail = True
    r = mon.run_cycle()
    assert r.sheet_written == 0 and store.seen[("TestCo", "A")]["passed_filter"] is None
    geo.fail = False
    r = mon.run_cycle()
    assert r.sheet_written == 1 and store.seen[("TestCo", "A")]["passed_filter"] is True


def test_geocoder_gives_up_after_max_attempts():
    mon, store, sheet, geo, *_ = make([L("A")])
    geo.fail = True
    for _ in range(CFG["geo_max_attempts"]):
        mon.run_cycle()
    assert len(sheet.rows) == 1
    assert sheet.rows[0][17] == "🟡 расстояние не определено"


def test_unknown_distance_is_written_yellow():
    mon, store, sheet, geo, *_ = make([L("A")])
    geo.km = None
    mon.run_cycle()
    assert sheet.rows[0][17] == "🟡 расстояние не определено"


def test_far_listing_rejected():
    mon, store, sheet, geo, *_ = make([L("A")])
    geo.km = 4.3
    mon.run_cycle()
    assert not sheet.rows and store.seen[("TestCo", "A")]["reject_reason"] == "далеко: 4.3 км > 3.5"


def test_relisting_after_14_days_is_new_row():
    mon, store, sheet, geo, parser, clock, _ = make([L("A")])
    mon.run_cycle()
    parser.items = []
    clock.t = T0 + dt.timedelta(days=1)
    mon.run_cycle()
    parser.items = [L("A", warm=610)]
    clock.t = T0 + dt.timedelta(days=20)
    r = mon.run_cycle()
    assert r.sheet_written == 1 and len(sheet.rows) == 2
    assert "повторная публикация, впервые видели 01.10.2026" in sheet.rows[1][19]
    assert store.listings[("TestCo", "A")]["warm"] == 610          # в базе обновили старую запись
    clock.t += dt.timedelta(minutes=3)
    store.seen[("TestCo", "A")]["written_sheet"] = False            # флаг потерян — дубля всё равно нет
    mon.run_cycle()
    assert len(sheet.rows) == 2


def test_short_gap_is_same_listing():
    mon, store, sheet, geo, parser, clock, _ = make([L("A")])
    mon.run_cycle()
    clock.t = T0 + dt.timedelta(days=5)
    mon.run_cycle()
    assert len(sheet.rows) == 1


def test_alert_only_on_transition_and_recovered():
    mon, store, sheet, geo, parser, clock, logs = make([L("A")])
    parser.fail = True
    alerts = [mon.run_cycle().alerts for _ in range(5)]
    assert alerts == [[], [], ["TestCo"], [], []]                  # красный — один раз, при переходе
    assert store.health["TestCo"]["state"] == "alert"
    assert any(m.startswith("[ALERT] TestCo") for m in logs)
    assert store.runs[-1]["error"].startswith("RuntimeError")
    parser.fail = False
    r = mon.run_cycle()
    assert r.recovered == ["TestCo"] and store.health["TestCo"]["state"] == "ok"
    assert any(m.startswith("[RECOVERED] TestCo") for m in logs)


def test_zero_items_counts_as_failure():
    mon, store, sheet, geo, parser, *_ = make([])
    assert [mon.run_cycle().alerts for _ in range(3)][-1] == ["TestCo"]


def test_broken_parser_does_not_stop_others():
    store, sheet, geo = FakeStore(), FakeSheet(), FakeGeo()
    bad = FakeParser([L("A")])
    bad.fail = True
    good = FakeParser([L("B", company="Other")])
    mon = Monitor(CFG, {"TestCo": bad, "Other": good}, store, sheet, geo, log=lambda m: None, clock=Clock())
    r = mon.run_cycle()
    assert r.stats["TestCo"].error and r.sheet_written == 1


def test_dry_run_writes_nothing():
    mon, store, sheet, *_ = make([L("A")], dry_run=True)
    mon.run_cycle()
    assert not sheet.rows and not store.listings and not store.seen and not store.runs


def test_legacy_link_match_counts_as_seen():
    mon, store, sheet, *_ = make([L("A")])
    store.seen[("TestCo", "legacy:https://example.org/A")] = {
        "company": "TestCo", "external_id": "legacy:https://example.org/A", "link": "https://example.org/A",
        "first_seen": T0.isoformat(), "last_seen": T0.isoformat(), "current_since": T0.isoformat(),
        "passed_filter": False, "reject_reason": "перенесено из seen.json",
        "written_sheet": True, "written_db": True, "legacy": True}
    r = mon.run_cycle()
    assert r.sheet_written == 0 and store.seen[("TestCo", "A")]["legacy"]
