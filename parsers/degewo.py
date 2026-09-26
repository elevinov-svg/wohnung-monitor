"""degewo: HTML-список с пагинацией + подробная страница.

В списке — только Warmmiete. На подробной странице: Nettokaltmiete,
Betriebskosten (kalt) = холодные Nebenkosten, Betriebskosten (warm) = отопление,
и адрес с индексом.

По умолчанию degewo сортирует по числу комнат, и при равенстве порядок между
запросами «плавает» — страницы пересекаются и часть квартир теряется. Поэтому
сначала переключаем сортировку на Warmmiete (состояние поиска хранится в cookie
сессии), потом идём по страницам.
"""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from models import Listing
from parsers.common import (MAX_PAGES, TIMEOUT, clean, get_detail, new_session, normalize_id, num,
                            page_lines, plz_of, price_pairs, value_after, wbs_from_text)

COMPANY = "degewo"
URL = "https://www.degewo.de/immosuche"
META: dict = {}   # счётчик сайта и т.п. последнего fetch() — для замеров


def fetch() -> list[Listing]:
    s = new_session()   # своя сессия: сортировка хранится в cookie

    def load(url):
        r = s.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")

    soup = load(URL)
    wbs_ids = _wbs_filter(s, soup)
    start = URL
    for opt in soup.select("select[name=sort-order] option"):
        if "warmmiete" in (opt.get("value") or ""):
            start = urljoin(URL, opt["value"].split("#")[0])
            break
    uniq, todo, done, total = {}, [start], set(), 0
    while todo and len(done) < MAX_PAGES:
        url = todo.pop(0)
        if url in done:
            continue
        done.add(url)
        soup = load(url)
        total = total or results_count(soup)
        for x in parse_list(soup):
            uniq.setdefault(x.external_id, x)
        pages = {}
        for a in soup.find_all("a", href=True):
            m = re.search(r"tx_openimmo_immobilie%5Bpage%5D=(\d+)", a["href"])
            if m:
                pages[int(m.group(1))] = urljoin(URL, a["href"].split("#")[0])
        for n in sorted(pages):
            if pages[n] not in done and pages[n] not in todo:
                todo.append(pages[n])
    if wbs_ids is not None:   # фильтр сайта «Wohnberechtigungsschein vorhanden»
        for x in uniq.values():
            x.wbs, x.wbs_source = ("да" if x.external_id in wbs_ids else "нет"), "фильтр сайта"
    META.clear()
    META.update(site_count=total or None, pages=len(done), wbs_filter=len(wbs_ids) if wbs_ids is not None else None)
    if total and len(uniq) < total:
        print(f"[warn] degewo: собрано {len(uniq)} из {total} — пагинация неполная")
    return list(uniq.values())


def _wbs_filter(s, soup) -> set | None:
    """ID объявлений, которые сайт показывает с галочкой «Wohnberechtigungsschein vorhanden»
    (поиск — POST-форма TYPO3 с __trustedProperties). None — если форма не найдена."""
    form = next((f for f in soup.find_all("form")
                 if f.find(attrs={"name": "tx_openimmo_immobilie[wbsSozialwohnung]"})), None)
    if form is None:
        print("[warn] degewo: форма поиска с фильтром WBS не найдена")
        return None
    data = [(i["name"], i.get("value", "")) for i in form.find_all("input")
            if i.get("name") and i.get("type") not in ("checkbox", "radio", "submit", "button")]
    data.append(("tx_openimmo_immobilie[wbsSozialwohnung]", "1"))
    ids, url = set(), urljoin(URL, form.get("action") or URL)
    r = s.post(url, data=data, timeout=TIMEOUT)
    r.raise_for_status()
    page = BeautifulSoup(r.text, "html.parser")
    ids |= {x.external_id for x in parse_list(page)}
    total = results_count(page)
    done = set()
    while len(ids) < total and len(done) < MAX_PAGES:   # обычно умещается на одной странице
        nxt = [urljoin(URL, a["href"]) for a in page.find_all("a", href=True)
               if "tx_openimmo_immobilie%5Bpage%5D=" in a["href"] and urljoin(URL, a["href"]) not in done]
        if not nxt:
            break
        done.add(nxt[0])
        r = s.get(nxt[0], timeout=TIMEOUT)
        page = BeautifulSoup(r.text, "html.parser")
        ids |= {x.external_id for x in parse_list(page)}
    # сбросить фильтр в сессии, иначе основной обход увидит только WBS-квартиры
    reset = [(k, v) for k, v in data if k != "tx_openimmo_immobilie[wbsSozialwohnung]"]
    s.post(url, data=reset, timeout=TIMEOUT)
    return ids


def results_count(soup) -> int:
    rc = soup.select_one(".results-count")
    return int(num(rc.get_text()) or 0) if rc else 0


def parse_list(soup) -> list[Listing]:
    if isinstance(soup, str):
        soup = BeautifulSoup(soup, "html.parser")
    out = []
    for t in soup.select(".c-teaser__inner"):
        a = t.select_one("h3 a[href]")
        btn = t.select_one("[data-openimmo-bookmark-item-uid]")
        if not a or not btn:
            continue
        link = urljoin(URL, a["href"])
        p = t.select_one(".c-copy > p")
        street, _, district = (p.get_text(" ", strip=True) if p else "").partition("|")
        facts = {}
        for div in t.select(".c-definition-list__item"):
            dt, dd = div.select_one("dt"), div.select_one("dd")
            if dt and dd:
                facts[dd.get_text(strip=True).lower()] = dt.get_text(" ", strip=True)
        tags = " ".join(x.get_text(" ", strip=True) for x in t.select(".c-tag__label"))
        title = clean(a.get_text(" ", strip=True))
        out.append(Listing(
            prices={"Warmmiete (список)": facts["warmmiete"]} if facts.get("warmmiete") else {},
            wbs_source="заголовок" if wbs_from_text(f"{title} {tags}") else None,
            extra={"list_raw": t.get_text(" | ", strip=True)[:2000]},
            company=COMPANY, external_id=normalize_id(btn["data-openimmo-bookmark-item-uid"]), link=link,
            title=title, address=clean(street), postcode=plz_of(street), district=clean(district),
            rooms=num(facts.get("zimmer")), area=num(facts.get("m²")), warm=num(facts.get("warmmiete")),
            wbs=wbs_from_text(f"{title} {tags}"), tags=" ".join(filter(None, [title, tags]))))
    return out


def enrich(x: Listing) -> None:
    parse_detail(get_detail(x.link), x)


def parse_detail(html: str, x: Listing) -> None:
    lines = page_lines(html)
    x.prices.update(price_pairs(lines))
    x.kalt = num(value_after(lines, "Nettokaltmiete")) or x.kalt
    x.neben = num(value_after(lines, "Betriebskosten (kalt)")) or x.neben
    x.heiz = num(value_after(lines, "Betriebskosten (warm)")) or x.heiz
    objektart = value_after(lines, "Objektart")
    if objektart:
        x.tags = f"{x.tags} {objektart}".strip()
    wbs = value_after(lines, "WBS", "Wohnberechtigungsschein", "WBS erforderlich")
    if wbs and x.wbs_source != "фильтр сайта":
        x.wbs = wbs_from_text(f"WBS: {wbs}") or x.wbs
        x.wbs_source = "подробная страница"
    addr = next((ln for ln in lines if ln.startswith("Adresse:")), None)
    if addr:
        full = clean(addr.removeprefix("Adresse:"))
        x.postcode = plz_of(full) or x.postcode
        x.address = full or x.address
    x.detail_loaded = True
