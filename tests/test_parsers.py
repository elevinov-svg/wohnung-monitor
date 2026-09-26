"""Разбор сохранённых страниц компаний. Если вёрстка сайта поменялась —
обновить образец в tests/fixtures и увидеть, что именно сломалось.
Значения сверены с живыми страницами 26.09.2026."""

import json
from pathlib import Path

import pytest

from models import Listing
from parsers import degewo, gesobau, gewobag, howoge, stadtundland, wbm
from parsers.common import normalize_id, num, wbs_from_text

F = Path(__file__).parent / "fixtures"


def html(name):
    return (F / name).read_text(encoding="utf-8")


def blank(company):
    return Listing(company=company, external_id="X", link="https://example.org")


# ----------------------------------------------------------------- утилиты

@pytest.mark.parametrize("text,expected", [
    ("1.423,09 €", 1423.09), ("45,52", 45.52), ("ca. 75.91 m²", 75.91), ("1.498", 1498.0),
    ("825,00 €", 825.0), ("70.36", 70.36), ("unbekannt", None), (None, None), (3, 3.0),
])
def test_num(text, expected):
    assert num(text) == expected


def test_normalize_id_keeps_separators():
    assert normalize_id(" w1300.42303.0131-0504 ") == "W1300.42303.0131-0504"
    assert normalize_id("1770-2403-555") != normalize_id("1770-24035-55")


@pytest.mark.parametrize("text,expected", [
    ("WBS erforderlich: Nein", "нет"), ("WBS: nein", "нет"), ("2 Zimmer Wohnung freifinanziert!", "нет"),
    ("mit WBS 160 oder WBS 180", "да"), ("WBS erforderlich: Ja", "да"), ("Helle Wohnung mit Balkon", None),
])
def test_wbs(text, expected):
    assert wbs_from_text(text) == expected


# ----------------------------------------------------------------- HOWOGE

def test_howoge_list():
    items = howoge.parse_list(json.loads(html("howoge_list.json")))
    assert len(items) == 37
    x = next(i for i in items if i.external_id == "1770-24035-55")
    assert x.address == "Franz-Schmidt-Straße 13, 13125 Berlin"
    assert (x.rooms, x.area, x.warm, x.wbs, x.postcode) == (2, 55, 1053, "нет", "13125")
    assert x.lat == pytest.approx(52.6328667)
    panketal = [i for i in items if "Panketal" in (i.address or "")]
    assert panketal and all(i.postcode == "16341" for i in panketal)


def test_howoge_project_cards():
    items = howoge.parse_project(html("howoge_project.html"))
    assert len(items) == 5
    x = items[0]
    assert x.external_id == "1771-10506-188"
    assert (x.address, x.rooms, x.area, x.warm, x.wbs) == ("Heiligenstadter Straße 2, 13055 Berlin", 2, 56, 1036, "нет")


def test_howoge_detail_rent_is_warm_and_heating_inside_nk():
    x = blank("HOWOGE")
    howoge.parse_detail(html("howoge_detail.html"), x)
    assert (x.kalt, x.neben, x.warm) == (825, 228, 1053)   # 825 + 228 = 1053 → «rent» = Warmmiete
    assert x.heiz is None and x.heiz_in_neben
    assert x.postcode == "13125"


# ----------------------------------------------------------------- degewo

def test_degewo_list():
    items = degewo.parse_list(html("degewo_list.html"))
    assert len(items) == 10
    assert degewo.results_count(__import__("bs4").BeautifulSoup(html("degewo_list.html"), "html.parser")) == 79
    x = items[0]
    assert x.external_id == "W1300.42303.0131-0504"
    assert (x.address, x.district, x.rooms, x.area, x.warm) == ("Zur Nachtheide 4", "Kietzer Feld", 1, 45.52, 970.02)


def test_degewo_detail():
    x = blank("degewo")
    degewo.parse_detail(html("degewo_detail.html"), x)
    assert (x.kalt, x.neben, x.heiz) == (327.77, 91.51, 67.73)
    assert not x.heiz_in_neben
    assert x.address == "Wittenberger Straße 21, 12689 Berlin" and x.postcode == "12689"
    assert x.wbs is None        # в меню сайта есть «WBS-Schnellcheck» — это не признак квартиры


# ----------------------------------------------------------------- WBM

def test_wbm_list():
    items = wbm.parse_list(html("wbm_list.html"))
    assert len(items) == 20
    x = items[0]
    assert x.external_id == "50-867500/10/144"
    assert (x.address, x.postcode, x.rooms, x.area, x.warm) == (
        "Konrad-Wolf-Strasse 42C, 13055 Berlin", "13055", 2, 75.91, 1449.88)


def test_wbm_detail():
    x = blank("WBM")
    wbm.parse_detail(html("wbm_detail.html"), x)
    assert (x.kalt, x.neben, x.warm, x.wbs) == (1131.06, 318.82, 1449.88, "нет")
    assert x.heiz_in_neben


# ----------------------------------------------------------------- GESOBAU

def test_gesobau_list():
    items = gesobau.parse_list(html("gesobau_list.html"))
    assert len(items) == 6
    x = items[0]
    assert x.external_id == "10-03247-00018-1343"
    assert (x.address, x.rooms, x.area, x.warm) == ("Ingeborg-Feustel-Straße 1, 12629 Berlin", 2, 57.82, 1150.62)


def test_gesobau_detail():
    x = blank("GESOBAU")
    gesobau.parse_detail(html("gesobau_detail.html"), x)
    assert (x.kalt, x.neben, x.heiz, x.warm, x.wbs) == (925.12, 138.77, 86.73, 1150.62, "нет")
    assert (x.lat, x.lon) == (52.53959, 13.598228)


# ----------------------------------------------------------------- Gewobag

def test_gewobag_list():
    items = gewobag.parse_list(html("gewobag_list.html"))
    assert len(items) == 20
    x = next(i for i in items if i.external_id == "0300-05633-0202-0033")
    assert (x.postcode, x.rooms, x.area, x.warm) == ("13627", 3, 66.69, 742.9)


def test_gewobag_detail():
    x = blank("Gewobag")
    gewobag.parse_detail(html("gewobag_detail.html"), x)
    assert (x.kalt, x.neben, x.heiz, x.warm) == (441.9, 171, 130, 742.9)
    assert (x.lat, x.lon) == (52.5356166, 13.2872451)
    assert x.postcode == "13627"


# ----------------------------------------------------------------- Stadt und Land

def test_stadtundland():
    rows = json.loads(html("stadtundland_list.json"))["data"]
    items = stadtundland.parse_list(rows)
    assert len(items) == 10
    x = items[0]
    assert x.external_id == "1001/5248/00243"
    assert x.link.endswith("1001%2F5248%2F00243")
    assert (x.kalt, x.neben, x.heiz, x.warm, x.rooms, x.area) == (1118.02, 158.31, 123.13, 1399.46, 3, 70.36)


def test_stadtundland_all_in_room_is_not_warm():
    rows = [{"headline": "Möbliertes Studentenzimmer - Bornsdorfer Str. 37B, Zi.2",
             "address": {"postal_code": "12053", "city": "Berlin", "street": "Bornsdorfer Str.", "house_number": "37B"},
             "details": {"immoNumber": "1001/5141/20942", "rooms": "1.00"},
             "costs": {"coldRent": "480", "totalRent": "480"}}]
    x = stadtundland.parse_list(rows)[0]
    assert x.kalt == 480 and x.warm is None


# ----------------------------------------------------------------- все ценовые поля и источник WBS

def test_prices_collected_as_is():
    x = blank("degewo")
    degewo.parse_detail(html("degewo_detail.html"), x)
    assert x.prices["Warmmiete"] == "487,01 €"          # сумма стоит перед меткой «Euro Warmmiete»
    assert x.prices["Betriebskosten (warm)"] == "67,73 €"
    assert x.prices["Kaution"] == "drei Nettokaltmieten"
    x = blank("Gewobag")
    gewobag.parse_detail(html("gewobag_detail.html"), x)
    assert set(x.prices) == {"Grundmiete", "VZ Betriebskosten kalt", "VZ Betriebskosten warm", "Gesamtmiete", "Kaution"}
    x = blank("GESOBAU")
    gesobau.parse_detail(html("gesobau_detail.html"), x)
    assert x.prices["Kaution"] == "2.775,36"


def test_stadtundland_keeps_discount_and_total():
    rows = json.loads(html("stadtundland_list.json"))["data"]
    rows[0]["costs"].update(discount="-385.40", totalRent="671,9")
    x = stadtundland.parse_list(rows)[:1][0]
    assert x.prices["discount (API)"] == "-385.40" and x.prices["totalRent (API)"] == "671,9"


def test_howoge_wbs_from_detail_for_project_units():
    x = howoge.parse_project(html("howoge_project.html"))[0]
    howoge.parse_detail(html("howoge_detail.html"), x)
    assert (x.wbs, x.wbs_source) == ("нет", "подробная страница")
    y = howoge.parse_list(json.loads(html("howoge_list.json")))[1]
    howoge.parse_detail(html("howoge_detail.html"), y)
    assert y.wbs_source == "поле API"


# ----------------------------------------------------------------- WBS: да / нет / неизвестно и тип

@pytest.mark.parametrize("lines,expected", [
    (["Bitte beachten Sie, dass zur Anmietung der Wohnung ein Wohnberechtigungsschein (WBS 160, 180 oder 220) "
      "benötigt wird (für zwei Räume)."], ("да", "160/180/220")),
    (["Mehr als die Hälfte aller Wohnungen sind vom Land Berlin gefördert."], (None, None)),   # неизвестно
    (["WBS-Schnellcheck", "Wohnberechtigungsschein (WBS)", "* Wohnberechtigungsschein.",
      "Hinweis: Alle Ergebnisse des WBS-Rechners sind ohne Gewähr."], (None, None)),            # меню/подвал
    (["Neubau Zweitbezug - Nähe Gärten der Welt - ohne WBS"], ("нет", None)),
])
def test_wbs_in_description(lines, expected):
    from parsers.common import wbs_in_description
    assert wbs_in_description(lines) == expected


@pytest.mark.parametrize("text,expected", [
    ("Coloniaallee 30, DG LI - WBS 140 mit besonderen Wohnbedarf!", "140 + besonderer Wohnbedarf"),
    ("*Neubau in Altglienicke – mit WBS 180, WBS 160, WBS 140*", "140/160/180"),
    ("Helle Wohnung im Dachgeschoss / Bad mit Wanne und Dusche / WBS 220", "220"),
    ("Helle 2-Zimmer-Wohnung mit Balkon", None),
])
def test_wbs_type(text, expected):
    from parsers.common import wbs_type
    assert wbs_type(text) == expected


def test_stadtundland_no_wbs_mention_is_unknown():
    rows = json.loads(html("stadtundland_list.json"))["data"]
    x = stadtundland.parse_list(rows)[0]
    assert x.wbs is None and x.wbs_source is None
