"""
Парсеры сайтов шести городских жилищных компаний Берлина
(landeseigene Wohnungsbaugesellschaften) и их общего портала inberlinwohnen.de.

Браузер не нужен: везде либо JSON, либо готовый HTML.

Каждая функция возвращает список словарей одного формата:
    key        — нормализованный ID объекта (одинаковый на сайте компании
                 и на inberlinwohnen.de — так мы понимаем, что это одна квартира)
    id, link, title, company, address, district, postcode,
    rooms, area, kalt, neben, heiz, warm, wbs, published
Неизвестные поля = None.
"""

import datetime
import json
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9"}
TIMEOUT = 30
MAX_PAGES = 20

_session = requests.Session()
_session.headers.update(HEADERS)


# ----------------------------------------------------------------- утилиты

def http_get(url: str, **kw) -> requests.Response:
    r = _session.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def http_post(url: str, **kw) -> requests.Response:
    r = _session.post(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def num(text) -> float | None:
    """'1.423,09 €' -> 1423.09 ; '45,52' -> 45.52 ; 'unbekannt' -> None"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"\d[\d.]*(?:,\d+)?|\d+(?:\.\d+)?", str(text))
    if not m:
        return None
    s = m.group(0)
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") == 1 and len(s.split(".")[1]) == 3:
        s = s.replace(".", "")  # '1.498' = тысяча
    try:
        return float(s)
    except ValueError:
        return None


def norm_key(object_id: str) -> str:
    """Общий ключ для сайта компании и портала: только буквы/цифры, нижний регистр."""
    return re.sub(r"[^0-9a-z]", "", (object_id or "").lower())


PLZ_RE = re.compile(r"\b(1[0-4]\d{3})\b")


def plz_of(text: str) -> str | None:
    m = PLZ_RE.search(text or "")
    return m.group(1) if m else None


def wbs_from_text(text: str) -> str | None:
    t = (text or "").lower()
    if re.search(r"ohne\s+wbs|kein\s+wbs|wbs\s+nicht\s+erforderlich|nicht\s+erforderlich", t):
        return "нет"
    if "wbs" in t or "wohnberechtigungsschein" in t:
        return "да"
    return None


def item(**kw) -> dict:
    base = dict(key=None, id=None, link=None, title=None, company=None, address=None,
                district=None, postcode=None, rooms=None, area=None, kalt=None,
                neben=None, heiz=None, warm=None, wbs=None, published=None)
    base.update(kw)
    if base["id"] is None:
        base["id"] = base["link"]
    if base.get("address"):
        base["address"] = re.sub(r"\s+", " ", base["address"]).strip()
    if base["postcode"] is None:
        base["postcode"] = plz_of(base.get("address") or "")
    return base


# ----------------------------------------------------------------- HOWOGE (JSON)

HOWOGE_URL = "https://www.howoge.de/?type=999&tx_howrealestate_json_list[action]=immoList"


def howoge() -> list[dict]:
    out, page = [], 1
    while page <= MAX_PAGES:
        form = {
            "tx_howrealestate_json_list[page]": str(page),
            "tx_howrealestate_json_list[limit]": "100",
            "tx_howrealestate_json_list[lang]": "",
            "tx_howrealestate_json_list[rent]": "",
            "tx_howrealestate_json_list[area]": "",
            "tx_howrealestate_json_list[rooms]": "egal",
            "tx_howrealestate_json_list[wbs]": "all-offers",
        }
        data = http_post(HOWOGE_URL, data=form).json()
        objs = data.get("immoobjects") or []
        out += [_howoge_item(o) for o in objs]
        total = int(data.get("immocount") or 0)
        if not objs or len(out) >= total:
            break
        page += 1
    return out


def _howoge_item(o: dict) -> dict:
    link = urljoin("https://www.howoge.de", o.get("link", ""))
    m = re.search(r"/detail/([\w-]+)\.html", link)
    oid = m.group(1) if m else str(o.get("uid"))
    wbs = {"ja": "да", "nein": "нет"}.get(str(o.get("wbs", "")).lower())
    return item(key=norm_key(oid), id=link, link=link, company="HOWOGE",
                title=o.get("notice") or o.get("title"), address=o.get("title"),
                district=o.get("district"), rooms=num(o.get("rooms")), area=num(o.get("area")),
                warm=num(o.get("rent")), wbs=wbs)


# ----------------------------------------------------------------- Stadt und Land (JSON)

SUL_URL = "https://d2396ha8oiavw0.cloudfront.net/sul-main/immoSearch"


def stadtundland() -> list[dict]:
    out, offset = [], 0
    for _ in range(MAX_PAGES):
        data = http_post(SUL_URL, json={"offset": offset, "cat": "wohnung"}).json()
        rows = data.get("data") or []
        out += [_sul_item(r) for r in rows]
        offset += len(rows)
        if not rows or offset >= int(data.get("count") or 0):
            break
    return out


def _sul_item(r: dict) -> dict:
    a, d, c = r.get("address", {}), r.get("details", {}), r.get("costs", {})
    oid = d.get("immoNumber", "")
    link = "https://stadtundland.de/wohnungssuche/" + requests.utils.quote(oid, safe="")
    addr = f'{a.get("street", "")} {a.get("house_number", "")}, {a.get("postal_code", "")} {a.get("city", "")}'.strip()
    return item(key=norm_key(oid), id=link, link=link, company="Stadt und Land",
                title=r.get("headline"), address=addr, district=a.get("precinct"),
                postcode=a.get("postal_code"), rooms=num(d.get("rooms")), area=num(d.get("livingSpace")),
                kalt=num(c.get("coldRent")), neben=num(c.get("additionalCosts")),
                heiz=num(c.get("heatingCosts")), warm=num(c.get("warmRent") or c.get("totalRent")),
                wbs=wbs_from_text(r.get("headline")))


# ----------------------------------------------------------------- degewo (HTML)

DEGEWO_URL = "https://www.degewo.de/immosuche"


def degewo() -> list[dict]:
    out, todo, done = [], [DEGEWO_URL], set()
    while todo and len(done) < MAX_PAGES:
        url = todo.pop(0)
        if url in done:
            continue
        done.add(url)
        soup = BeautifulSoup(http_get(url).text, "html.parser")
        for t in soup.select(".c-teaser__inner"):
            it = _degewo_item(t)
            if it:
                out.append(it)
        # пагинация TYPO3: ссылки с cHash, берём все номера страниц
        pages = {}
        for a in soup.find_all("a", href=True):
            m = re.search(r"tx_openimmo_immobilie%5Bpage%5D=(\d+)", a["href"])
            if m:
                pages[int(m.group(1))] = urljoin(DEGEWO_URL, a["href"].split("#")[0])
        for n in sorted(pages):
            if pages[n] not in done and pages[n] not in todo:
                todo.append(pages[n])
    # дубли (первая страница может открыться по двум URL)
    uniq = {}
    for it in out:
        uniq.setdefault(it["key"], it)
    return list(uniq.values())


def _degewo_item(t) -> dict | None:
    a = t.select_one("h3 a[href]")
    if not a:
        return None
    link = urljoin(DEGEWO_URL, a["href"])
    btn = t.select_one("[data-openimmo-bookmark-item-uid]")
    oid = btn["data-openimmo-bookmark-item-uid"] if btn else link
    p = t.select_one(".c-copy > p")
    addr_line = p.get_text(" ", strip=True) if p else ""
    street, _, district = addr_line.partition("|")
    facts = {}
    for div in t.select(".c-definition-list__item"):
        dt, dd = div.select_one("dt"), div.select_one("dd")
        if dt and dd:
            facts[dd.get_text(strip=True).lower()] = dt.get_text(" ", strip=True)
    tags = " ".join(x.get_text(" ", strip=True) for x in t.select(".c-tag__label"))
    title = a.get_text(" ", strip=True)
    return item(key=norm_key(oid), id=link, link=link, company="degewo", title=title,
                address=street.strip(), district=district.strip() or None,
                rooms=num(facts.get("zimmer")), area=num(facts.get("m²")),
                warm=num(facts.get("warmmiete")), wbs=wbs_from_text(title + " " + tags))


# ----------------------------------------------------------------- WBM (HTML, одна страница)

WBM_URL = "https://www.wbm.de/wohnungen-berlin/angebote/"


def wbm() -> list[dict]:
    soup = BeautifulSoup(http_get(WBM_URL).text, "html.parser")
    out = []
    for row in soup.select(".openimmo-search-list-item"):
        a = row.select_one("a.immo-button-cta[href]") or row.select_one("a[href*='/details/']")
        if not a:
            continue
        link = urljoin(WBM_URL, a["href"])
        title_el = row.select_one(".imageTitle")
        addr_el = row.select_one(".address")
        area_el = row.select_one(".area")
        checks = " ".join(li.get_text(" ", strip=True) for li in row.select(".check-property-list li"))
        title = title_el.get_text(" ", strip=True) if title_el else ""
        out.append(item(
            key=norm_key(row.get("data-id") or link), id=link, link=link, company="WBM",
            title=title, address=addr_el.get_text(" ", strip=True) if addr_el else None,
            district=area_el.get_text(strip=True) if area_el else None,
            rooms=num(_text(row, ".main-property-rooms")), area=num(_text(row, ".main-property-size")),
            warm=num(_text(row, ".main-property-rent")), wbs=wbs_from_text(title + " " + checks)))
    return out


def _text(node, sel):
    el = node.select_one(sel)
    return el.get_text(" ", strip=True) if el else None


# ----------------------------------------------------------------- GESOBAU (HTML, пагинация solr)

GESOBAU_URL = "https://www.gesobau.de/mieten/wohnungssuche/"
GESOBAU_ID_RE = re.compile(r"(\d{2}-\d{5}-\d{5}-\d{4}-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def gesobau() -> list[dict]:
    out, keys = [], set()
    for page in range(1, MAX_PAGES + 1):
        url = GESOBAU_URL if page == 1 else f"{GESOBAU_URL}?tx_solr%5Bpage%5D={page}"
        soup = BeautifulSoup(http_get(url).text, "html.parser")
        new = 0
        for e in soup.select(".results-entry"):
            it = _gesobau_item(e)
            if it and it["key"] not in keys:
                keys.add(it["key"])
                out.append(it)
                new += 1
        if new == 0:
            break
    return out


def _gesobau_item(e) -> dict | None:
    a = e.select_one(".basicTeaser__title a[href]")
    if not a:
        return None
    link = urljoin(GESOBAU_URL, a["href"])
    m = GESOBAU_ID_RE.search(link)
    warm = rooms = area = None
    for p in (e.select_one(".apartment__info").find_all("span") if e.select_one(".apartment__info") else []):
        s = p.get_text(" ", strip=True)
        if "€" in s:
            warm = num(s)
        elif "Zimmer" in s:
            rooms = num(s)
        elif "m²" in s:
            area = num(s)
    title = a.get_text(" ", strip=True)
    tags = " ".join(x.get_text(" ", strip=True) for x in e.select(".tags li"))
    return item(key=norm_key(m.group(1) if m else link), id=link, link=link, company="GESOBAU",
                title=title, address=_text(e, ".basicTeaser__text"), district=_text(e, ".meta__region"),
                rooms=rooms, area=area, warm=warm, wbs=wbs_from_text(title + " " + tags))


# ----------------------------------------------------------------- Gewobag (HTML, /page/N/)

GEWOBAG_URL = "https://www.gewobag.de/fuer-mietinteressentinnen/mietangebote/"
GEWOBAG_QS = "?objekttyp%5B%5D=wohnung&sort-by=recent"


def gewobag() -> list[dict]:
    out, keys = [], set()
    for page in range(1, MAX_PAGES + 1):
        url = GEWOBAG_URL + ("" if page == 1 else f"page/{page}/") + GEWOBAG_QS
        r = _session.get(url, timeout=TIMEOUT)
        if r.status_code == 404:
            break
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        new = 0
        for art in soup.select("article.angebot-big-box"):
            it = _gewobag_item(art)
            if it and it["key"] not in keys:
                keys.add(it["key"])
                out.append(it)
                new += 1
        if new == 0:
            break
    return out


def _gewobag_item(art) -> dict | None:
    link = None
    for a in art.select("a[href]"):
        if re.search(r"/mietangebote/\d{4}-\d{5}-\d{4}-\d{4}", a["href"]):
            link = a["href"]
            break
    if not link:
        return None
    oid = re.search(r"/mietangebote/(\d{4}-\d{5}-\d{4}-\d{4})", link).group(1)
    addr = _text(art, ".angebot-address address")
    title = _text(art, ".angebot-title") or ""
    area_txt = _text(art, ".angebot-area td") or ""
    rooms = num(area_txt.split("|")[0]) if "Zimmer" in area_txt else None
    area = num(area_txt.split("|")[1]) if "|" in area_txt else None
    chars = _text(art, ".angebot-characteristics") or ""
    return item(key=norm_key(oid), id=link, link=link, company="Gewobag", title=title,
                address=addr, district=_text(art, ".angebot-region td"), rooms=rooms, area=area,
                warm=num(_text(art, ".angebot-kosten td")), wbs=wbs_from_text(title + " " + chars))


# ----------------------------------------------------------------- inberlinwohnen.de (портал)

IBW_URL = "https://www.inberlinwohnen.de/wohnungsfinder/"
COMPANY_BY_HOST = {
    "stadtundland": "Stadt und Land", "degewo": "degewo", "gesobau": "GESOBAU",
    "gewobag": "Gewobag", "howoge": "HOWOGE", "wbm": "WBM", "berlinovo": "berlinovo",
}


def inberlinwohnen() -> list[dict]:
    """Первая страница портала (10 самых свежих объявлений, сортировка по дате).
    Нам нужна именно свежесть: при частых проверках первой страницы хватает."""
    soup = BeautifulSoup(http_get(IBW_URL).text, "html.parser")
    out, keys = [], set()
    for a in soup.find_all("a", attrs={"wire:snapshot": True}):
        try:
            data = json.loads(a["wire:snapshot"]).get("data", {})
        except (ValueError, KeyError):
            continue
        if "deeplink" not in data:
            continue
        box = a.find_parent(class_="md:flex") or a.parent
        oid = data.get("objectId") or ""
        key = norm_key(oid)
        if key in keys:
            continue
        keys.add(key)
        facts = {}
        for row in box.select(".results__row"):
            dts, dds = row.select("dt"), row.select("dd")
            for dt, dd in zip(dts, dds):
                facts[dt.get_text(strip=True).rstrip(":").lower()] = dd.get_text(" ", strip=True)
        link = data["deeplink"]
        host = re.sub(r"^www\.", "", requests.utils.urlparse(link).hostname or "")
        company = next((v for k, v in COMPANY_BY_HOST.items() if k in host), host)
        img = box.find("img")
        uploaded = None
        m = re.search(r"/(\d{10})-", img["src"]) if img and img.get("src") else None
        if m:
            uploaded = datetime.datetime.fromtimestamp(int(m.group(1)), datetime.timezone.utc)
        wbs_raw = next((v for k, v in facts.items() if "wbs" in k or "wohnberechtigung" in k), None)
        wbs = None
        if wbs_raw:
            wbs = "нет" if "nicht" in wbs_raw.lower() else ("да" if "erforderlich" in wbs_raw.lower() else wbs_raw)
        addr = facts.get("adresse")
        title_el = box.find(["h2", "h3"])
        out.append(item(
            key=key, id=link, link=link, company=company,
            title=title_el.get_text(" ", strip=True) if title_el else addr,
            address=addr, district=addr.split(",")[-1].strip() if addr else None,
            rooms=num(facts.get("zimmeranzahl")), area=num(facts.get("wohnfläche")),
            kalt=num(facts.get("kaltmiete")), neben=num(facts.get("nebenkosten")),
            # «Gesamtmiete» без известных Nebenkosten = просто Kaltmiete, не путаем с Warmmiete
            warm=num(facts.get("gesamtmiete")) if num(facts.get("nebenkosten")) else None, wbs=wbs,
            published=facts.get("eingestellt am"),
            portal_uploaded=uploaded.isoformat() if uploaded else None))
    return out


COMPANY_CHECKERS = {
    "howoge": howoge,
    "degewo": degewo,
    "wbm": wbm,
    "gesobau": gesobau,
    "gewobag": gewobag,
    "stadtundland": stadtundland,
    "inberlinwohnen": inberlinwohnen,
}
