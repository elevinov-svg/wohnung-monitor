"""GESOBAU: HTML-список (пагинация solr) + подробная страница.

В списке — «Miete inkl. NK» (Warmmiete). На подробной странице: Kaltmiete,
Betriebskosten (холодные), Heizkosten, «WBS: ja/nein», координаты.
Стабильный ID — Objektnummer из ссылки ('10-03247-00018-1343'); хвост-UUID
в ссылке к квартире не привязан и при перевыставлении может смениться.
"""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from models import Listing
from parsers.common import (MAX_PAGES, clean, coords_from_html, get, get_detail, normalize_id, num,
                            page_lines, plz_of, price_pairs, value_after, wbs_from_text)

COMPANY = "GESOBAU"
URL = "https://www.gesobau.de/mieten/wohnungssuche/"
ID_RE = re.compile(r"(\d{2}-\d{5}-\d{5}-\d{4})-[0-9a-f]{8}-")
META: dict = {}


def fetch() -> list[Listing]:
    uniq, max_page, pages = {}, 1, 0
    for page in range(1, MAX_PAGES + 1):
        url = URL if page == 1 else f"{URL}?tx_solr%5Bpage%5D={page}"
        new = 0
        html = get(url).text
        pages += 1
        max_page = max([max_page] + [int(n) for n in re.findall(r"tx_solr%5Bpage%5D=(\d+)", html)])
        for x in parse_list(html):
            if x.external_id not in uniq:
                uniq[x.external_id] = x
                new += 1
        if new == 0:
            break
    META.clear()
    # счётчика на сайте нет; по пагинации: max_page страниц по 6 объявлений
    META.update(site_count=None, site_pages=max_page, pages=pages)
    return list(uniq.values())


def parse_list(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for e in soup.select(".results-entry"):
        a = e.select_one(".basicTeaser__title a[href]")
        if not a:
            continue
        link = urljoin(URL, a["href"])
        m = ID_RE.search(link)
        if not m:
            continue
        warm = rooms = area = None
        warm_raw = None
        info = e.select_one(".apartment__info")
        for sp in info.find_all("span") if info else []:
            s = sp.get_text(" ", strip=True)
            if "€" in s:
                warm, warm_raw = num(s), s
            elif "Zimmer" in s:
                rooms = num(s)
            elif "m²" in s:
                area = num(s)
        title = clean(a.get_text(" ", strip=True))
        tags = " ".join(x.get_text(" ", strip=True) for x in e.select(".tags li"))
        address = clean(_text(e, ".basicTeaser__text"))
        out.append(Listing(
            prices={"Miete inkl. NK (список)": warm_raw} if warm_raw else {},
            wbs_source="заголовок" if wbs_from_text(f"{title} {tags}") else None,
            extra={"list_raw": e.get_text(" | ", strip=True)[:2000]},
            company=COMPANY, external_id=normalize_id(m.group(1)), link=link,
            title=title, address=address, postcode=plz_of(address), district=clean(_text(e, ".meta__region")),
            rooms=rooms, area=area, warm=warm, wbs=wbs_from_text(f"{title} {tags}"),
            tags=" ".join(filter(None, [title, tags]))))
    return out


def enrich(x: Listing) -> None:
    parse_detail(get_detail(x.link), x)


def parse_detail(html: str, x: Listing) -> None:
    lines = page_lines(html)
    x.prices.update(price_pairs(lines))
    x.kalt = num(value_after(lines, "Kaltmiete")) or x.kalt
    x.neben = num(value_after(lines, "Betriebskosten")) or x.neben
    x.heiz = num(value_after(lines, "Heizkosten")) or x.heiz
    x.warm = num(value_after(lines, "Miete inkl. NK")) or x.warm
    wbs_line = next((ln for ln in lines if re.match(r"WBS:\s*\S", ln)), None)
    if wbs_line:
        x.wbs = wbs_from_text(wbs_line) or x.wbs
        x.wbs_source = "подробная страница"
    c = coords_from_html(html)
    if c and x.lat is None:
        x.lat, x.lon = c
    x.detail_loaded = True


def _text(node, sel):
    el = node.select_one(sel)
    return el.get_text(" ", strip=True) if el else None
