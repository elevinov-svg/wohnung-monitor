"""HOWOGE: JSON-список + страницы новостроек + подробная страница.

Один запрос к JSON отдаёт все обычные квартиры (page/limit сервер игнорирует).
Квартиры в новостройках в JSON не попадают — там только карточки проектов
(projectteaser). Их страницы читаем отдельно: список проектов берём из того же
JSON, поэтому новые проекты подхватываются сами. На странице проекта у каждой
квартиры уже есть комнаты и Warmmiete — этого хватает для грубого фильтра.

Поле "rent" в JSON — Warmmiete (на сайте подписано «Warmmiete»).
На подробной странице: Kaltmiete, Nebenkosten (с отоплением внутри), Warmmiete.
"""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from models import Listing
from parsers.common import (clean, get, get_detail, normalize_id, num, page_lines, plz_of, post,
                            price_pairs, value_after, wbs_from_text)

COMPANY = "HOWOGE"
BASE = "https://www.howoge.de"
LIST_URL = BASE + "/?type=999&tx_howrealestate_json_list[action]=immoList"
FORM = {
    "tx_howrealestate_json_list[page]": "1",
    "tx_howrealestate_json_list[limit]": "12",
    "tx_howrealestate_json_list[lang]": "",
    "tx_howrealestate_json_list[rooms]": "",
    "tx_howrealestate_json_list[wbs]": "",
}
ID_RE = re.compile(r"/wohnungssuche/detail/([\w-]+)\.html")
META: dict = {}


def fetch() -> list[Listing]:
    data = post(LIST_URL, data=FORM).json()
    out = parse_list(data)
    known = {x.external_id for x in out}
    project_units = 0
    for proj in data.get("projectteaser") or []:
        url = urljoin(BASE, proj.get("link") or "")
        if not url.startswith(BASE):
            continue
        for x in parse_project(get(url).text):
            project_units += 1
            if x.external_id not in known:
                known.add(x.external_id)
                out.append(x)
    expected = int(data.get("immocount") or 0)
    META.clear()
    META.update(site_count=expected or None, main=len(data.get("immoobjects") or []),
                projects=len(data.get("projectteaser") or []), project_units=project_units,
                teasercount=data.get("teasercount"))
    if expected and len(out) < expected:
        print(f"[warn] HOWOGE: собрано {len(out)}, сайт пишет {expected} "
              f"(обычных {len(data.get('immoobjects') or [])}, в новостройках {project_units})")
    return out


def parse_list(data: dict) -> list[Listing]:
    out = []
    for o in data.get("immoobjects") or []:
        link = urljoin(BASE, o.get("link") or "")
        m = ID_RE.search(link)
        if not m:
            continue
        coords = o.get("coordinates") or {}
        title = clean(o.get("notice"))
        api_wbs = {"ja": "да", "nein": "нет"}.get(str(o.get("wbs", "")).lower())
        out.append(Listing(
            prices={"rent (список, = Warmmiete)": str(o.get("rent"))} if o.get("rent") is not None else {},
            wbs_source="поле API" if api_wbs else ("заголовок" if wbs_from_text(title) else None),
            extra={"list_raw": o},
            company=COMPANY, external_id=normalize_id(m.group(1)), link=link,
            title=title, address=clean(o.get("title")), postcode=plz_of(o.get("title")),
            district=clean(o.get("district")), rooms=num(o.get("rooms")), area=num(o.get("area")),
            warm=num(o.get("rent")),
            wbs=api_wbs or wbs_from_text(title),
            lat=num(coords.get("lat")), lon=num(coords.get("lng")),
            tags=title or ""))   # только заголовок: в «features» бывает «Stellplatz» у обычных квартир
    return out


def parse_project(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("a.flat-single[href]"):
        link = urljoin(BASE, a["href"])
        m = ID_RE.search(link)
        if not m:
            continue
        facts = {}
        for block in a.select(".attributes > div"):
            head, content = block.select_one(".attributes-headline"), block.select_one(".attributes-content")
            if head and content:
                facts[head.get_text(strip=True).lower()] = content.get_text(" ", strip=True)
        title = clean(_text(a, ".notice"))
        address = clean(_text(a, ".address"))
        out.append(Listing(
            prices={"Warmmiete (карточка проекта)": facts["warmmiete"]} if facts.get("warmmiete") else {},
            wbs_source="заголовок" if wbs_from_text(title) else None,
            extra={"list_raw": a.get_text(" | ", strip=True)[:2000], "howoge_project": True},
            company=COMPANY, external_id=normalize_id(m.group(1)), link=link,
            title=title, address=address, postcode=plz_of(address), district=clean(_text(a, ".district")),
            rooms=num(facts.get("zimmer")), area=num(facts.get("wohnfläche")),
            warm=num(facts.get("warmmiete")), wbs=wbs_from_text(title),
            tags=" ".join(filter(None, [title, "Neubau"]))))
    return out


def enrich(x: Listing) -> None:
    parse_detail(get_detail(x.link), x)


def parse_detail(html: str, x: Listing) -> None:
    lines = page_lines(html)
    x.prices.update(price_pairs(lines))
    kalt = num(value_after(lines, "Kaltmiete"))
    neben = num(value_after(lines, "Nebenkosten"))
    heiz = num(value_after(lines, "Heizkosten"))
    warm = num(value_after(lines, "Warmmiete"))
    x.kalt = kalt if kalt is not None else x.kalt
    x.warm = warm if warm is not None else x.warm
    if neben is not None:
        x.neben = neben
        if heiz is not None:
            x.heiz = heiz
        else:
            x.heiz_in_neben = True   # отопление отдельно не указано: Kalt + NK = Warm
    x.rooms = x.rooms if x.rooms is not None else num(value_after(lines, "Zimmer"))
    x.area = x.area if x.area is not None else num(value_after(lines, "Fläche"))
    if not x.postcode and "Adresse:" in lines:
        i = lines.index("Adresse:")          # «Adresse: / Straße 13, / 13125 Berlin, Buch»
        x.postcode = plz_of(" ".join(lines[i + 1:i + 3]))
    if x.wbs_source != "поле API":
        # у обычных квартир проверено 37 из 37: строка «WBS erforderlich» есть ⇔ в API wbs=ja
        x.wbs = "да" if "WBS erforderlich" in lines else "нет"
        x.wbs_source = "подробная страница"
    x.detail_loaded = True


def _text(node, sel):
    el = node.select_one(sel)
    return el.get_text(" ", strip=True) if el else None
