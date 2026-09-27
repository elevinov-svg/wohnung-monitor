"""Gewobag: HTML-список (/page/N/) + подробная страница.

В списке — Gesamtmiete (= Warmmiete). На подробной странице: Grundmiete,
VZ Betriebskosten kalt (холодные Nebenkosten), VZ Betriebskosten warm (отопление),
координаты. ID — Objektnummer из ссылки ('0300-05633-0202-0033').
"""

import re

from bs4 import BeautifulSoup

from models import Listing
from parsers.common import (MAX_PAGES, TIMEOUT, clean, coords_from_html, get_detail, normalize_id, num,
                            page_lines, plz_of, price_pairs, require, session, value_after, wbs_from_text,
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
        if r.status_code == 404 and page > 1:   # страницы кончились
            break
        r.raise_for_status()                    # 404 на первой странице — поломка, а не «0 объявлений»
        if page == 1:
            # контейнер списка есть всегда, в т.ч. когда по фильтру ничего не найдено
            require("filtered-mietangebote" in r.text, f"нет контейнера списка filtered-mietangebote ({qs[:60]})")
            require("angebot-big-box" not in r.text or parse_list(r.text), "карточки есть, но ни одна не разобрана")
        max_page = max([max_page] + [int(n) for n in re.findall(r"/mietangebote/page/(\d+)/", r.text)])
        new = 0
        for x in parse_list(r.text):
            if x.external_id not in uniq:
                uniq[x.external_id] = x
                new += 1
        if new == 0:
            break
    return uniq, max_page, pages


def fetch(known: set | None = None, force_wbs: bool = False) -> list[Listing]:
    """known — уже известные ID: если новых нет (и не force_wbs), фильтры WBS не обходим,
    WBS известных объявлений берётся из базы (x.extra["wbs_filters_skipped"])."""
    uniq, max_page, pages = _crawl(QS)
    META.clear()
    if known is not None and not force_wbs and set(uniq) <= known:
        for x in uniq.values():
            x.extra["wbs_filters_skipped"] = True
        META.update(site_count=None, site_pages=max_page, pages=pages, wbs_filters="пропущены: новых ID нет")
        return list(uniq.values())
    # WBS — из двух фильтров сайта: «WBS» (wohnungstyp[]=wbs) и «kein WBS nötig» (keinwbs=1).
    # Только в одном — да/нет. В обоих (26.09: 3 квартиры) — фильтры противоречат, остаётся
    # то, что сказано в заголовке/описании, иначе неизвестно. Ни в одном — тоже неизвестно.
    no_wbs, _, pages2 = _crawl(QS + "&keinwbs=1")
    with_wbs, _, pages3 = _crawl(QS + "&wohnungstyp%5B%5D=wbs")
    for x in uniq.values():
        yes, no = x.external_id in with_wbs, x.external_id in no_wbs
        if yes != no:
            x.wbs, x.wbs_source = ("да" if yes else "нет"), "фильтр сайта"
        elif yes and no:
            x.extra["wbs_filters_conflict"] = True
    META.update(site_count=None, site_pages=max_page, pages=pages + pages2 + pages3, wbs_filters="обойдены",
                keinwbs=len(no_wbs), wbs=len(with_wbs), wbs_conflict=len(set(no_wbs) & set(with_wbs)))
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
