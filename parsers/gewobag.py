"""Gewobag: HTML-список (/page/N/) + подробная страница.

В списке — Gesamtmiete (= Warmmiete). На подробной странице: Grundmiete,
VZ Betriebskosten kalt (холодные Nebenkosten), VZ Betriebskosten warm (отопление),
координаты. ID — Objektnummer из ссылки ('0300-05633-0202-0033').
"""

import re

from bs4 import BeautifulSoup

from models import Listing
from parsers.common import (MAX_PAGES, TIMEOUT, clean, coords_from_html, get_detail, normalize_id, num,
                            page_lines, plz_of, price_pairs, session, value_after, wbs_from_text,
                            wbs_in_description, wbs_type)

COMPANY = "Gewobag"
URL = "https://www.gewobag.de/fuer-mietinteressentinnen/mietangebote/"
QS = "?objekttyp%5B%5D=wohnung&sort-by=recent"
ID_RE = re.compile(r"/mietangebote/(\d{4}-\d{5}-\d{4}-\d{4})")
META: dict = {}


def _crawl(qs: str) -> tuple[dict, int, int]:
    uniq, max_page, pages = {}, 1, 0
    for page in range(1, MAX_PAGES + 1):
        r = session.get(URL + ("" if page == 1 else f"page/{page}/") + qs, timeout=TIMEOUT)
        pages += 1
        if r.status_code == 404:   # страницы кончились
            break
        r.raise_for_status()
        max_page = max([max_page] + [int(n) for n in re.findall(r"/mietangebote/page/(\d+)/", r.text)])
        new = 0
        for x in parse_list(r.text):
            if x.external_id not in uniq:
                uniq[x.external_id] = x
                new += 1
        if new == 0:
            break
    return uniq, max_page, pages


def fetch() -> list[Listing]:
    uniq, max_page, pages = _crawl(QS)
    # WBS — из фильтра сайта «kein WBS nötig»: кто не попал в него, тому WBS нужен
    no_wbs, _, pages2 = _crawl(QS + "&keinwbs=1")
    if uniq and len(no_wbs) <= len(uniq):
        for x in uniq.values():
            x.wbs, x.wbs_source = ("нет" if x.external_id in no_wbs else "да"), "фильтр сайта"
    META.clear()
    META.update(site_count=None, site_pages=max_page, pages=pages + pages2, keinwbs=len(no_wbs))
    return list(uniq.values())


def parse_list(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for art in soup.select("article.angebot-big-box"):
        link = next((a["href"] for a in art.select("a[href]") if ID_RE.search(a["href"])), None)
        if not link:
            continue
        address = clean(_text(art, ".angebot-address address"))
        title = clean(_text(art, ".angebot-title"))
        area_txt = _text(art, ".angebot-area td") or ""
        rooms_txt, _, area = area_txt.partition("|")
        chars = _text(art, ".angebot-characteristics") or ""
        cost = _text(art, ".angebot-kosten td")
        out.append(Listing(
            prices={"Gesamtmiete (список)": cost} if cost else {},
            wbs_source="заголовок" if wbs_from_text(f"{title} {chars}") else None,
            extra={"list_raw": art.get_text(" | ", strip=True)[:2000]},
            company=COMPANY, external_id=normalize_id(ID_RE.search(link).group(1)), link=link,
            title=title, address=address, postcode=plz_of(address), district=clean(_text(art, ".angebot-region td")),
            rooms=num(rooms_txt) if "Zimmer" in rooms_txt else None, area=num(area),
            warm=num(_text(art, ".angebot-kosten td")), wbs=wbs_from_text(f"{title} {chars}"),
            wbs_type=wbs_type(f"{title} {chars}"),
            tags=title or ""))
    return out


def enrich(x: Listing) -> None:
    parse_detail(get_detail(x.link), x)


def parse_detail(html: str, x: Listing) -> None:
    lines = page_lines(html)
    x.prices.update(price_pairs(lines))
    x.kalt = num(value_after(lines, "Grundmiete")) or x.kalt
    x.neben = num(value_after(lines, "VZ Betriebskosten kalt", "Betriebskosten kalt")) or x.neben
    x.heiz = num(value_after(lines, "VZ Betriebskosten warm", "Betriebskosten warm", "Heizkosten")) or x.heiz
    x.warm = num(value_after(lines, "Gesamtmiete")) or x.warm
    addr = value_after(lines, "Anschrift")
    if addr:
        x.postcode = plz_of(addr) or x.postcode
    for ln in lines:
        if x.wbs_source == "фильтр сайта":
            break
        if re.search(r"(WBS|Wohnberechtigungsschein).*(erforderlich|notwendig|benötigt)", ln, re.I):
            x.wbs = wbs_from_text(ln) or x.wbs
            x.wbs_source = "подробная страница"
            break
    x.wbs_type = x.wbs_type or wbs_in_description(lines)[1]
    c = coords_from_html(html)
    if c and x.lat is None:
        x.lat, x.lon = c
    x.detail_loaded = True


def _text(node, sel):
    el = node.select_one(sel)
    return el.get_text(" ", strip=True) if el else None
