"""Сбор: поля подробной страницы, Kaution, проверка структуры, повторы, изменения."""

import argparse
from pathlib import Path

import pytest
import requests

import collect
from models import Listing
from parsers import common, degewo, gewobag, stadtundland
from parsers.common import StructureError, detail_extras, detail_pairs, fill_deposit

FIX = Path(__file__).parent / "fixtures"


def html(name):
    return (FIX / name).read_text(encoding="utf-8")


def test_detail_extras_gewobag():
    x = Listing(company="Gewobag", external_id="1", link="")
    page = html("gewobag_detail.html").replace(
        "</head>", '<script type="application/ld+json">{"@type":"WebPage",'
                   '"datePublished":"2026-09-25T16:55:03+00:00"}</script></head>', 1)
    detail_extras(page, x, detail_pairs(page))
    assert x.published == "2026-09-25T16:55:03+00:00"
    assert x.lat and x.lon


def test_detail_extras_degewo_skips_bracket():
    x = Listing(company="degewo", external_id="1", link="")
    page = html("degewo_detail.html")
    detail_extras(page, x, detail_pairs(page))
    assert x.available_from == "sofort"
    assert x.floor == "4 von 6"


@pytest.mark.parametrize("prices, amount, text", [
    ({"Kaution": "3.354,06"}, 3354.06, "3.354,06"),
    ({"deposit (API)": "2.971,8"}, 2971.8, "2.971,8"),
    ({"Kaution": "3402,00 €"}, 3402.0, "3402,00 €"),
    ({"Kaution": "drei Nettokaltmieten"}, None, "drei Nettokaltmieten"),
    ({"Kaution": "Die Kaution beträgt 3 Mieten"}, None, "Die Kaution beträgt 3 Mieten"),
    ({"Kaltmiete": "500 €"}, None, None),
])
def test_fill_deposit(prices, amount, text):
    x = Listing(company="c", external_id="1", link="", prices=prices)
    fill_deposit(x)
    assert (x.deposit, x.deposit_text) == (amount, text)


class FakeResp:
    def __init__(self, status=200, text="", data=None):
        self.status_code, self.text, self._data = status, text, data
        self.headers = {"content-type": "text/html"}

    def json(self):
        if self._data is None:
            raise ValueError("no JSON")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


def test_gewobag_404_on_first_page_is_error(monkeypatch):
    monkeypatch.setattr(gewobag, "session", type("S", (), {"get": lambda *a, **k: FakeResp(404)})())
    with pytest.raises(requests.HTTPError):
        gewobag.fetch()


def test_gewobag_page_without_container_is_structure_error(monkeypatch):
    monkeypatch.setattr(gewobag, "session", type("S", (), {"get": lambda *a, **k: FakeResp(200, "<html></html>")})())
    with pytest.raises(StructureError):
        gewobag.fetch()


def test_gewobag_skips_wbs_filters_when_no_new_ids(monkeypatch):
    calls = []
    page = html("gewobag_list.html")

    def get(url, **kw):
        calls.append(url)
        return FakeResp(200, page) if "page/" not in url else FakeResp(404)

    monkeypatch.setattr(gewobag, "session", type("S", (), {"get": lambda self, url, **kw: get(url)})())
    ids = {x.external_id for x in gewobag.parse_list(page)}
    items = gewobag.fetch(known=ids)
    assert len(calls) == 2 and not any("wbs" in c for c in calls)
    assert all(x.extra.get("wbs_filters_skipped") for x in items)
    calls.clear()
    gewobag.fetch(known=ids - {next(iter(ids))})       # один новый ID -> фильтры обходятся
    assert any("keinwbs" in c for c in calls) and any("wohnungstyp" in c for c in calls)


def test_stadtundland_missing_keys_is_structure_error(monkeypatch):
    monkeypatch.setattr(stadtundland, "post", lambda *a, **k: FakeResp(200, data={"items": []}))
    with pytest.raises(StructureError):
        stadtundland.fetch()


def test_stadtundland_valid_empty_is_not_error(monkeypatch):
    monkeypatch.setattr(stadtundland, "post", lambda *a, **k: FakeResp(200, data={"data": [], "count": 0}))
    assert stadtundland.fetch() == []


def test_degewo_page_without_counter_is_structure_error(monkeypatch):
    class S:
        headers, hooks = {}, {"response": []}

        def get(self, *a, **k):
            return FakeResp(200, "<html><body>Wartungsarbeiten</body></html>")

    monkeypatch.setattr(degewo, "new_session", S)
    with pytest.raises(StructureError):
        degewo.fetch()


def test_retry_on_timeout(monkeypatch):
    attempts = []

    def fake(self, method, url, **kw):
        attempts.append(kw.get("timeout"))
        if len(attempts) < 3:
            raise requests.Timeout()
        r = requests.Response()
        r.status_code = 200
        return r

    monkeypatch.setattr(requests.Session, "request", fake)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    before = common.stats()["retries"]
    assert common.new_session().get("https://example.invalid").status_code == 200
    assert attempts == [common.TIMEOUT] * 3
    assert common.stats()["retries"] - before == 2


def test_retry_gives_up_after_two(monkeypatch):
    def fake(self, method, url, **kw):
        raise requests.ConnectionError()

    monkeypatch.setattr(requests.Session, "request", fake)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    with pytest.raises(requests.ConnectionError):
        common.new_session().get("https://example.invalid")


# ------------------------------------------------------------------ логика цикла на поддельной базе

class FakeDB:
    def __init__(self):
        self.listings, self.changes, self.runs, self.health = {}, [], [], {}

    configured = True

    def select(self, table, params):
        company = params["company"].removeprefix("eq.")
        if table == "collected_listings":
            return [dict(r) for r in self.listings.values() if r["company"] == company]
        return [dict(self.health[company])] if company in self.health else []

    def upsert(self, table, rows, on_conflict):
        for r in rows:
            if table == "parser_health":
                self.health[r["company"]] = dict(r)
            else:
                self.listings.setdefault(r["external_id"], {}).update(r)

    def insert(self, table, rows):
        (self.changes if table == "listing_changes" else self.runs).extend(rows)

    def rpc(self, fn, args):
        ids, ts, out = set(args["p_ids"]), args["p_ts"], []
        for eid, r in self.listings.items():
            if eid in ids:
                if r.get("disappeared_at"):
                    out.append({"external_id": eid, "event": "back"})
                r.update(last_seen=ts, disappeared_at=None)
            elif args["p_mark_gone"] and not r.get("disappeared_at"):
                r["disappeared_at"] = ts
                out.append({"external_id": eid, "event": "gone"})
        return out


class FakeParser:
    COMPANY = "Test"
    META: dict = {}

    def __init__(self):
        self.items, self.detail = [], {}

    def fetch(self):
        return [Listing(**{**vars(x), "prices": dict(x.prices), "extra": dict(x.extra)}) for x in self.items]

    def parse_detail(self, html, x):
        for k, v in self.detail.get(x.external_id, {}).items():
            setattr(x, k, v)


def make_worker(monkeypatch):
    monkeypatch.setattr(collect, "get_detail", lambda url: "<html></html>")
    args = argparse.Namespace(no_db=True, interval=180)
    w = collect.Worker("Test", FakeParser(), args, end=0)
    w.db = FakeDB()
    return w


def test_cycle_new_change_gone_and_suspect(monkeypatch):
    w = make_worker(monkeypatch)
    p = w.parser
    p.items = [Listing(company="Test", external_id="A", link="a", warm=500.0),
               Listing(company="Test", external_id="B", link="b", warm=600.0)]
    p.detail = {"A": {"kalt": 400.0}, "B": {"kalt": 450.0}}
    w.cycle()
    db = w.db
    assert db.listings["A"]["baseline"] is True and db.listings["A"]["kalt"] == 400.0
    assert db.runs[-1]["new_count"] == 2 and not db.changes

    # цена в списке изменилась, B пропало; kalt в списке нет — это не изменение
    p.items = [Listing(company="Test", external_id="A", link="a", warm=520.0)]
    w.cycle()
    fields_changed = [(c["external_id"], c["field"], c["old_value"], c["new_value"]) for c in db.changes]
    assert fields_changed == [("A", "warm", 500.0, 520.0)]
    assert db.listings["A"]["kalt"] == 400.0
    assert db.listings["B"]["disappeared_at"] and db.runs[-1]["gone_count"] == 1

    # новое объявление после первого цикла — не baseline, подробная сразу
    p.items.append(Listing(company="Test", external_id="C", link="c", warm=700.0))
    p.detail["C"] = {"kalt": 550.0}
    w.cycle()
    assert db.listings["C"]["baseline"] is False and db.listings["C"]["detail_ok"] is True

    # пусто после непустого — suspect, исчезновения не отмечаются; повторно пусто — empty
    p.items = []
    w.cycle()
    assert db.runs[-1]["status"] == "suspect" and not db.listings["A"]["disappeared_at"]
    w.cycle()
    assert db.runs[-1]["status"] == "empty" and db.listings["A"]["disappeared_at"]


def test_alert_after_three_errors(monkeypatch):
    w = make_worker(monkeypatch)

    def boom():
        raise requests.Timeout()

    w.parser.fetch = boom
    for _ in range(3):
        w.cycle()
    assert w.alerted and w.db.health["Test"]["state"] == "alert"
    assert [r["status"] for r in w.db.runs] == ["error"] * 3
