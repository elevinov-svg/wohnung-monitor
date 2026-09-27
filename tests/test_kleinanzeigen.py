"""Kleinanzeigen: разбор страниц (образцы в tests/fixtures, персональные данные заменены)
и логика обхода лент — без браузера и без базы. Структура сверена с сайтом 27.09.2026."""

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

import kleinanzeigen_collect as kc
from models import Listing
from parsers import kleinanzeigen as ka
from parsers.common import StructureError

F = Path(__file__).parent / "fixtures"
MITTE = "https://www.kleinanzeigen.de/s-wohnung-mieten/mitte/sortierung:neuste/anzeige:angebote/preis::850/c203l3518"


def html(name):
    return (F / name).read_text(encoding="utf-8")


def blank(eid="3524524449"):
    return Listing(company=ka.COMPANY, external_id=eid, link=f"https://www.kleinanzeigen.de/s-anzeige/x/{eid}-203-9670")


# ----------------------------------------------------------------- URL поиска

def test_search_url_filters_in_site_format():
    base = dict(location_slug="friedrichshain", location_id=3350, max_price=850, rooms=(1, 1.5))
    assert ka.search_url(**base) == ("https://www.kleinanzeigen.de/s-wohnung-mieten/friedrichshain/sortierung:neuste/"
                                     "anzeige:angebote/preis::850/c203l3350+wohnung_mieten.zimmer_d:1,1.5")
    assert ka.search_url(**base, wbs_filter=True).endswith("c203l3350+wohnung_mieten.zimmer_d:1,1.5+wohnung_mieten.wbs_b:true")
    assert "/preis::850/wbs/k0c203l3350+" in ka.search_url(**base, keyword="WBS")


def test_ad_id_from_link():
    assert ka.ad_id("https://www.kleinanzeigen.de/s-anzeige/schoene-wohnung/3524524449-203-9670") == "3524524449"
    assert ka.ad_id("https://www.kleinanzeigen.de/s-wohnung-mieten/mitte/c203l3518") is None


# ----------------------------------------------------------------- список

def test_parse_list():
    page = ka.parse_list(html("kleinanzeigen_list.html"), MITTE)
    ids = [x.external_id for x in page.items]
    assert ids == ["3100000001", "3524524449", "3524487890", "3497316411", "3395645309"]
    assert "3299805021" not in ids                    # «похожие» из других районов — не берём
    assert page.total == 207
    assert "/seite:2/" in page.next_url

    top, private, _, tausch, _ = page.items
    assert top.extra["top"] is True and private.extra["top"] is False
    assert private.link.endswith("/3524524449-203-9670")
    assert (private.postcode, private.district, private.area, private.rooms, private.kalt) == ("10553", "Mitte", 50, 1.5, 560)
    assert private.seller_type == "private" and tausch.seller_type is None
    # «(ca.200 € Nebenkosten)» в описании не должно стать ценой
    assert (tausch.kalt, tausch.area, tausch.rooms) == (550, 40, 1.5)
    assert top.area == 24 and top.rooms == 1          # «24 m² · 1 Zi. · Online-Besichtigung»


def test_parse_list_next_page_from_page_2():
    page = ka.parse_list(html("kleinanzeigen_list.html"), MITTE.replace("preis::850/", "preis::850/seite:2/"))
    assert "/seite:3/" in page.next_url


def test_parse_list_empty_is_not_an_error():
    page = ka.parse_list(html("kleinanzeigen_list_empty.html"))
    assert page.items == [] and page.total == 0


def test_parse_list_unknown_structure():
    with pytest.raises(StructureError):
        ka.parse_list("<html><body><p>Wartungsarbeiten</p></body></html>")


def test_block_detected():
    with pytest.raises(ka.BlockDetected):
        ka.parse_list(html("kleinanzeigen_block.html"))
    with pytest.raises(ka.BlockDetected):
        ka.parse_list(html("kleinanzeigen_list.html"), status=429)
    with pytest.raises(ka.BlockDetected):
        ka.parse_detail(html("kleinanzeigen_block.html"), blank())


# ----------------------------------------------------------------- подробная

def test_detail_private_from_page_json():
    x = blank()
    ka.parse_detail(html("kleinanzeigen_detail_private.html"), x)
    assert x.title == "Schöne 1,5 Zimmerwohnung ab sofort in Berlin Tiergarten!"
    assert x.description.startswith("Schöne 1,5-Zimmer-Wohnung in Berlin – Musterstraße\n")
    assert "Kaution 3 Kaltmieten." in x.description                   # целиком, без обрезки
    assert (x.kalt, x.warm, x.neben, x.heiz) == (560, 760, None, None)
    assert x.prices == {"Kaltmiete (Preis)": "560 €", "Warmmiete": "760 €"}
    assert (x.area, x.rooms, x.floor, x.available_from) == (50, 1.5, "4", "Oktober 2026")
    assert (x.postcode, x.district, x.address) == ("10553", "Mitte", "10553 Berlin - Mitte")
    assert x.published == "2026-09-27"
    assert (x.seller_type, x.seller_name, x.contact_phone, x.contact_email) == ("private", "Max", None, None)
    assert x.contact["user_id"] == "11111111" and x.contact["user_since"] == "09.10.2014"
    assert x.contact["form"] == {"available": True, "login_required": True, "fields": ["contactName", "phoneNumber"]}
    assert x.contact["phone_login_required"] is True
    assert x.extra["detail_raw"]["wbs_in_text"] is False
    assert x.extra["detail_raw"]["tags"] == ["Balkon", "Badewanne", "Altbau"]
    assert x.wbs is None and x.detail_loaded


def test_detail_commercial_imprint_and_creation_date():
    x = blank("3497316411")
    ka.parse_detail(html("kleinanzeigen_detail_commercial.html"), x)
    assert x.seller_type == "commercial" and x.seller_name == "Tauschwohnung GmbH"
    assert x.contact_phone == "+49 228 000000" and x.contact_email == "support@example.com"
    assert x.contact["website"] == "https://www.tauschwohnung.com"
    assert x.contact["profile_url"] == "https://www.kleinanzeigen.de/pro/Tauschwohnung-GmbH"
    assert "53111 Bonn" in x.contact["imprint"]
    # поднятое объявление: дата создания из JSON, дата поднятия — отдельно
    assert x.published == "2026-08-28"
    assert x.extra["detail_raw"]["date_shown"] == "2026-09-27"
    assert x.extra["detail_raw"]["bumped_at"].startswith("2026-09-")


def test_detail_legacy_page_without_json():
    x = blank("3497316411")
    ka.parse_detail(html("kleinanzeigen_detail_commercial_legacy.html"), x)
    assert x.kalt == 550 and x.extra["detail_raw"]["price_negotiable"] is True
    assert (x.neben, x.heiz, x.warm, x.deposit_text) == (200, 60, None, "1.650 €")
    assert x.address == "Musterstraße 1, 13353 Mitte - Wedding" and x.district == "Wedding"
    assert x.seller_type == "commercial" and x.contact["user_since"] == "12.03.2021"
    assert x.contact["form"]["available"] is True
    assert x.published == "2026-09-27"
    assert x.extra["detail_raw"]["wbs_in_text"] is True
    assert (x.wbs, x.wbs_source) == ("да", "метка на сайте")
    assert x.description.endswith("WBS 140 erforderlich.")


def test_detail_removed_ad():
    with pytest.raises(StructureError):
        ka.parse_detail("<html><body><div id='srchrslt-adtable'></div></body></html>", blank())


# ----------------------------------------------------------------- обход лент

class FakeBrowser:
    def __init__(self, pages: dict):
        self.pages, self.requests, self.visited = pages, 0, []

    def get(self, url):
        self.requests += 1
        self.visited.append(url)
        return self.pages[url], 200, url


CFG = {"page_pause": (0, 0), "stop_after_known": 1}


def feed(kind="general", url=MITTE):
    return kc.Feed(f"mitte:{kind}", kind, url)


def test_scan_stops_on_first_known_but_not_on_top():
    b = FakeBrowser({MITTE: html("kleinanzeigen_list.html")})
    found = {}
    known = {"3100000001", "3524487890"}               # TOP (известный) и третье объявление
    meta = kc.scan_feed(b, feed(), known, found, CFG, max_pages=5)
    assert list(found) == ["3524524449"]              # TOP пропущен, стоп на 3524487890
    assert meta["stop"] == "известный ID 3524487890" and meta["pages"] == 1
    assert len(b.visited) == 1                        # вторую страницу не открывали


def test_scan_follows_pages_until_limit():
    page2 = MITTE.replace("preis::850/", "preis::850/seite:2/") + "+wohnung_mieten.zimmer_d:1%2C1.5"
    b = FakeBrowser({MITTE: html("kleinanzeigen_list.html"), page2: html("kleinanzeigen_list_empty.html")})
    meta = kc.scan_feed(b, feed(), set(), {}, CFG, max_pages=1)
    assert meta["stop"] == "лимит 1 стр." and len(b.visited) == 1
    meta = kc.scan_feed(b, feed(), set(), {}, CFG, max_pages=5)
    assert meta["stop"] == "конец ленты" and meta["pages"] == 2


def test_scan_merges_feeds_by_id_and_marks_wbs_filter():
    wbs_url = MITTE + "+wohnung_mieten.wbs_b:true"
    b = FakeBrowser({MITTE: html("kleinanzeigen_list.html"), wbs_url: html("kleinanzeigen_list.html")})
    found = {}
    kc.scan_feed(b, feed(), set(), found, CFG, max_pages=1)
    kc.scan_feed(b, feed("wbs_filter", wbs_url), set(), found, CFG, max_pages=1)
    assert len(found) == 5                            # дубликатов нет
    x = found["3524524449"]
    assert x.found_via == ["mitte:general", "mitte:wbs_filter"]
    assert (x.wbs, x.wbs_source) == ("да", "фильтр сайта")


def test_make_feeds():
    feeds = kc.make_feeds({"locations": [{"name": "mitte", "slug": "mitte", "id": 3518}], "max_price": 850,
                           "rooms": [1, 1.5], "wbs_keyword": "wbs"})
    assert [f.kind for f in feeds] == ["general", "wbs_filter", "wbs_text"]
    assert "wohnung_mieten.wbs_b:true" in feeds[1].url and "/wbs/k0" in feeds[2].url


def test_db_row_has_generic_columns():
    x = blank()
    ka.parse_detail(html("kleinanzeigen_detail_private.html"), x)
    x.found_via = ["mitte:general"]
    x.extra.update(detail_ok=True)
    row = kc.db_row(x, "2026-09-27T10:00:00+00:00", baseline=False)
    assert row["company"] == "Kleinanzeigen" and row["external_id"] == "3524524449"
    for col in ("description", "seller_type", "seller_name", "contact_phone", "contact_email", "contact", "found_via"):
        assert col in row
    assert row["detail_checked_at"] == "2026-09-27T10:00:00+00:00" and row["baseline"] is False
    assert "extra" not in row and "detail_loaded" not in row


# ----------------------------------------------------------------- браузер недоступен / пауза

def run_args(**kw):
    return SimpleNamespace(now=False, no_db=True, max_details=None, **kw)


KC = {"cdp_url": "http://127.0.0.1:9", "retry_hours": 2, "block_retry_hours": 6, "start_jitter": 0}


def test_browser_unavailable_defers_two_hours(tmp_path, monkeypatch):
    monkeypatch.setattr(kc, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(kc, "cdp_alive", lambda url: False)
    assert kc.run(run_args(), KC) == 0
    st = kc.load_state()
    delay = dt.datetime.fromisoformat(st["next_try_after"]) - kc.utcnow()
    assert dt.timedelta(hours=1, minutes=59) < delay <= dt.timedelta(hours=2)
    assert "браузер недоступен" in st["deferred_reason"]


def test_runs_before_retry_time_exit_quietly(tmp_path, monkeypatch):
    monkeypatch.setattr(kc, "STATE", tmp_path / "state.json")
    kc.save_state({"next_try_after": (kc.utcnow() + dt.timedelta(hours=1)).isoformat(), "deferred_reason": "тест"})
    monkeypatch.setattr(kc, "cdp_alive", lambda url: pytest.fail("не должен проверять браузер во время паузы"))
    assert kc.run(run_args(), KC) == 0
