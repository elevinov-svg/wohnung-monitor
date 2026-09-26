"""WBM: одна HTML-страница со всеми предложениями + подробная страница.

В списке — Warmmiete. На подробной странице: Nettokaltmiete, Nebenkosten
(отопление внутри, отдельно не указано), Warmmiete, «WBS erforderlich: Ja/Nein».
Стабильный ID — Objektnummer в атрибуте data-id ('50-867500/10/144');
ссылка же — slug из заголовка и может меняться.
"""

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from models import Listing
from parsers.common import (clean, get, get_detail, normalize_id, num, page_lines, plz_of,
                            price_pairs, value_after, wbs_from_text)

COMPANY = "WBM"
URL = "https://www.wbm.de/wohnungen-berlin/angebote/"
META: dict = {}


def fetch() -> list[Listing]:
    out = parse_list(get(URL).text)
    META.clear()
    META.update(site_count=None, pages=1)   # счётчика на сайте нет, всё на одной странице
    return out


def parse_list(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for row in soup.select(".openimmo-search-list-item[data-id]"):
        a = row.select_one("a.immo-button-cta[href]") or row.select_one("a[href*='/details/']")
        if not a:
            continue
        title = clean(_text(row, ".imageTitle"))
        address = clean(_text(row, ".address"))
        checks = " ".join(li.get_text(" ", strip=True) for li in row.select(".check-property-list li"))
        rent = _text(row, ".main-property-rent")
        out.append(Listing(
            prices={"Warmmiete (список)": rent} if rent else {},
            wbs_source="заголовок" if wbs_from_text(f"{title} {checks}") else None,
            extra={"list_raw": row.get_text(" | ", strip=True)[:2000], "uid": row.get("data-uid")},
            company=COMPANY, external_id=normalize_id(row["data-id"]), link=urljoin(URL, a["href"]),
            title=title, address=address, postcode=plz_of(address), district=clean(_text(row, ".area")),
            rooms=num(_text(row, ".main-property-rooms")), area=num(_text(row, ".main-property-size")),
            warm=num(_text(row, ".main-property-rent")), wbs=wbs_from_text(f"{title} {checks}"),
            tags=title or ""))
    return out


def enrich(x: Listing) -> None:
    parse_detail(get_detail(x.link), x)


def parse_detail(html: str, x: Listing) -> None:
    lines = page_lines(html)
    x.prices.update(price_pairs(lines))
    kaution = next((ln for ln in lines if ln.startswith("Die Kaution beträgt")), None)
    if kaution:
        x.prices.setdefault("Kaution", kaution)
    x.kalt = num(value_after(lines, "Nettokaltmiete", "Kaltmiete")) or x.kalt
    heiz = num(value_after(lines, "Heizkosten"))
    neben = num(value_after(lines, "Nebenkosten", "Betriebskosten"))
    if neben is not None:
        x.neben = neben
        x.heiz = heiz
        x.heiz_in_neben = heiz is None
    x.warm = num(value_after(lines, "Warmmiete")) or x.warm
    wbs = value_after(lines, "WBS erforderlich")
    if wbs:
        x.wbs = wbs_from_text(f"WBS erforderlich: {wbs}") or x.wbs
        x.wbs_source = "подробная страница"
    x.detail_loaded = True


def _text(node, sel):
    el = node.select_one(sel)
    return el.get_text(" ", strip=True) if el else None
