"""
Kleinanzeigen (группа C): разбор страниц. Здесь только разбор HTML: без браузера,
без базы и без оценки «подходит / нет» — этим занимается фильтр (отдельный этап).

Структура сайта (проверено 27.09.2026):

  список   /s-wohnung-mieten/<район>/sortierung:neuste/anzeige:angebote/preis::<max>/
           [<слово>/k0]c203l<id района>+wohnung_mieten.zimmer_d:<от>,<до>[+wohnung_mieten.wbs_b:true]
           - preis:: — фильтр сайта по Kaltmiete;
           - wohnung_mieten.wbs_b:true — чекбокс «WBS» в фильтрах сайта;
           - <слово>/k0 — полнотекстовый поиск (заголовок + описание);
           - результаты — ul#srchrslt-adtable; ul#srchrslt-adtable-altads — «похожие»
             из других районов, их НЕ берём;
           - TOP-объявления (платное закрепление) стоят первыми и при сортировке по дате;
           - поднятые (hochgeschoben) старые объявления тоже стоят вверху — у них старый ID;
           - даты в списке нет.
  подробная /s-anzeige/<slug>/<ID>-203-<район>
           - ID — число (10 цифр), «Anzeigen-ID» на странице;
           - в браузерной версии у кнопки контакта (astro-island ContactButton) лежит
             JSON объявления: продавец, телефон, Impressum, форма отклика, дата создания;
           - без него (упрощённая версия страницы) — разбор по id элементов #viewad-*.
"""

import datetime as dt
import html as htmlmod
import json
import re
from dataclasses import dataclass, field
from urllib.parse import quote

from bs4 import BeautifulSoup

from models import Listing
from parsers.common import StructureError, clean, num, plz_of

COMPANY = "Kleinanzeigen"
BASE = "https://www.kleinanzeigen.de"
CATEGORY = 203                    # Mietwohnungen

ID_RE = re.compile(r"/(\d{6,})-\d+-\d+/?$")
WBS_WORD = re.compile(r"\bwbs\b|wohnberechtigungsschein", re.I)
PHONE_RE = re.compile(r"(?:telefon|tel\.?|phone|mobil)\s*:?\s*(\+?[\d][\d /()-]{5,}\d)", re.I)
# подписи в блоке продавца, которые не являются именем
NOT_A_NAME = re.compile(r"anzeigen online|^folgen$|zufriedenheit|freundlich|zuverlässig|aktiv seit|nutzer$", re.I)
EMAIL_RE =re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# признаки капчи / блокировки: при них парсер останавливается целиком (BlockDetected)
BLOCK_MARKERS = re.compile(
    r"captcha|sicherheitsabfrage|sicherheitsüberprüfung|bist du ein mensch|bist du ein roboter"
    r"|ungewöhnlich viele anfragen|zugriff verweigert|access denied|request blocked"
    r"|captcha-delivery\.com|px-captcha|datadome", re.I)
NO_RESULTS = re.compile(r"keine ergebnisse", re.I)
# строки карточки списка целиком: «50 m² · 1,5 Zi.», «560 €», «1.295 € VB»
SIZE_LINE = re.compile(r"^[\d.,]+\s*m²(?:\s*·\s*[\d.,]+\s*Zi\.)?(?:\s*·.*)?$|^[\d.,]+\s*Zi\.(?:\s*·.*)?$")
PRICE_LINE = re.compile(r"^[\d.,]+\s*€(?:\s*VB)?$|^VB$")


class BlockDetected(Exception):
    """Капча / блокировка. Сканирование останавливается целиком, без пропуска страниц."""


# ================================================================ URL поиска

def search_url(location_slug: str, location_id: int, max_price: float | None = None,
               rooms: tuple[float, float] | None = None, wbs_filter: bool = False,
               keyword: str | None = None) -> str:
    """Первая страница поиска, в каноническом виде сайта (так сайт сам строит ссылки)."""
    path = f"/s-wohnung-mieten/{location_slug}/sortierung:neuste/anzeige:angebote/"
    if max_price:
        path += f"preis::{int(max_price)}/"
    if keyword:
        path += f"{quote(keyword.strip().lower())}/k0"
    path += f"c{CATEGORY}l{location_id}"
    if rooms:
        path += f"+wohnung_mieten.zimmer_d:{_n(rooms[0])},{_n(rooms[1])}"
    if wbs_filter:
        path += "+wohnung_mieten.wbs_b:true"
    return BASE + path


def _n(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


def ad_id(link: str | None) -> str | None:
    m = ID_RE.search(link or "")
    return m.group(1) if m else None


# ================================================================ блокировка

def check_block(html: str, status: int | None = None) -> None:
    if status in (403, 429):
        raise BlockDetected(f"HTTP {status}")
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("#srchrslt-adtable, #viewad-main, #viewad-title"):
        return                              # нормальная страница (слово «captcha» может быть в скриптах)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = soup.get_text(" ", strip=True)[:5000]
    if BLOCK_MARKERS.search(title) or BLOCK_MARKERS.search(text) or BLOCK_MARKERS.search(html[:20000]):
        raise BlockDetected(f"капча / блокировка: «{(title or text)[:120]}»")


# ================================================================ список

@dataclass
class ListPage:
    items: list[Listing] = field(default_factory=list)
    total: int | None = None           # «1 - 25 von 207 Ergebnissen»
    next_url: str | None = None


def parse_list(html: str, page_url: str = "", status: int | None = None) -> ListPage:
    check_block(html, status)
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#srchrslt-adtable")
    summary = soup.select_one("#srp-breadcrumb-summary, .breadcrump-summary")
    if table is None:
        if NO_RESULTS.search(soup.get_text(" ", strip=True)):
            return ListPage(total=0)
        raise StructureError("нет списка результатов (#srchrslt-adtable) и нет «keine Ergebnisse»")

    page = ListPage()
    if summary:
        m = re.search(r"von\s+([\d.]+)", summary.get_text(" ", strip=True))
        page.total = int(m.group(1).replace(".", "")) if m else None

    for art in table.select("article[data-adid]"):
        if art.find_parent(id="srchrslt-adtable-altads"):
            continue
        page.items.append(parse_list_item(art))

    # следующая страница — по ссылке сайта, а не собираем URL сами
    m = re.search(r"/seite:(\d+)/", page_url)
    cur = int(m.group(1)) if m else 1
    nxt = soup.select_one(f'a[href*="/seite:{cur + 1}/"]')
    if nxt:
        page.next_url = BASE + nxt["href"] if nxt["href"].startswith("/") else nxt["href"]
    return page


def parse_list_item(art) -> Listing:
    eid = art.get("data-adid")
    href = art.get("data-href") or (art.select_one("a[href*='/s-anzeige/']") or {}).get("href")
    if not eid or not href:
        raise StructureError("в карточке списка нет data-adid / ссылки")
    link = BASE + href if href.startswith("/") else href
    x = Listing(company=COMPANY, external_id=str(eid), link=link)

    h = art.select_one("h2 a, h3 a, .ellipsis")
    x.title = clean(h.get_text(" ", strip=True)) if h else None

    # «10553 Mitte» — индекс и район; «50 m² · 1,5 Zi.»; «560 €»
    texts = [clean(t) for t in art.stripped_strings]
    texts = [t for t in texts if t and not t.startswith("{")]          # без JSON-LD
    for t in texts:
        if x.postcode is None and re.match(r"^\d{5}\s", t):
            x.postcode = t[:5]
            x.district = clean(t[5:])
            x.address = t
        elif SIZE_LINE.match(t) and x.area is None and x.rooms is None:
            for part in t.split("·"):
                if "m²" in part:
                    x.area = num(part.replace("m²", ""))
                elif "Zi" in part:
                    x.rooms = num(part.replace("Zi.", ""))
        elif PRICE_LINE.match(t) and x.kalt is None:
            x.prices["Preis (Liste)"] = t
            x.kalt = num(t.replace("VB", "").replace("€", ""))
    if any(t == "Von Privat" for t in texts):
        x.seller_type = "private"
    x.extra["top"] = any(t == "TOP" for t in texts)
    x.extra["list_raw"] = {"texts": texts[:12]}
    x.tags = x.title or ""
    return x


# ================================================================ подробная страница

def astro_props(value):
    """Свойства astro-island: каждое значение — [тип, значение], тип 1 = массив."""
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
        kind, v = value
        return [astro_props(i) for i in v] if kind == 1 else astro_props(v)
    if isinstance(value, dict):
        return {k: astro_props(v) for k, v in value.items()}
    return value


def ad_data(soup) -> dict | None:
    isl = soup.select_one('astro-island[component-url*="ContactButton"]')
    if isl is None or not isl.get("props"):
        return None
    try:
        props = astro_props(json.loads(isl["props"]))
    except (ValueError, TypeError):
        return None
    data = props.get("adData") if isinstance(props, dict) else None
    return data if isinstance(data, dict) else None


def html_text(fragment: str | None) -> str | None:
    if not fragment:
        return None
    text = re.sub(r"<br\s*/?>", "\n", fragment)
    text = BeautifulSoup(text, "html.parser").get_text()
    return htmlmod.unescape(text).strip() or None


def ddmmyyyy(text: str | None) -> str | None:
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def parse_detail(html: str, x: Listing, status: int | None = None) -> None:
    """Дочитать подробную страницу в объявление. Поля, которых нет, не трогаются."""
    check_block(html, status)
    soup = BeautifulSoup(html, "html.parser")
    title = soup.select_one("#viewad-title")
    data = ad_data(soup)
    if title is None and data is None:
        raise StructureError("нет заголовка (#viewad-title) и данных объявления — удалено или другая вёрстка")
    data = data or {}
    raw: dict = {}

    x.title = clean(data.get("title")) or (clean(title.get_text(" ", strip=True)) if title else x.title)
    desc = soup.select_one("#viewad-description-text")
    if desc is not None:
        for br in desc.find_all("br"):
            br.replace_with("\n")
        x.description = desc.get_text().strip() or None
    if not x.description and data.get("description"):
        x.description = html_text(data["description"])

    # --- адрес: «10553 Berlin - Mitte», улица — только если продавец её указал
    loc = soup.select_one("#viewad-locality")
    street = soup.select_one("#street-address")
    loc_t = clean(loc.get_text(" ", strip=True)) if loc else None
    street_t = clean(street.get_text(" ", strip=True).rstrip(",")) if street else None
    if loc_t:
        x.postcode = plz_of(loc_t) or x.postcode
        x.district = clean(loc_t.split(" - ")[-1]) if " - " in loc_t else x.district
        x.address = ", ".join(filter(None, [street_t, loc_t]))

    # --- атрибуты: Wohnfläche, Zimmer, Etage, Warmmiete, Nebenkosten, Heizkosten, Kaution …
    attrs = {}
    for a in data.get("localizedAttributes") or []:
        if isinstance(a, dict) and a.get("localizedName"):
            attrs[a["localizedName"]] = a.get("localizedValue")
    if not attrs:
        for li in soup.select("#viewad-details li"):
            # «Wohnfläche<span class=…--value>50 m²</span>» — метка текстом, значение в span
            parts = [p for p in (clean(t) for t in li.stripped_strings) if p]
            if len(parts) >= 2:
                attrs[parts[0]] = parts[-1]
    raw["attributes"] = attrs

    price_el = soup.select_one("#viewad-price")
    price_t = clean(price_el.get_text(" ", strip=True)) if price_el else None
    if price_t:
        x.prices["Kaltmiete (Preis)"] = price_t          # поле «Preis» у Mietwohnungen = Kaltmiete
        x.kalt = num(price_t.replace("VB", "").replace("€", ""))
        raw["price_negotiable"] = "VB" in price_t
    for k, v in attrs.items():
        kl = k.lower()
        if "€" in (v or "") or any(w in kl for w in ("miete", "kosten", "kaution")):
            x.prices[k] = v
        if kl == "wohnfläche":
            x.area = num((v or "").replace("m²", ""))
        elif kl == "zimmer":
            x.rooms = num(v)
        elif kl == "etage":
            x.floor = v
        elif kl.startswith("verfügbar"):
            x.available_from = v
        elif kl == "warmmiete":
            x.warm = num((v or "").replace("€", ""))
        elif kl == "nebenkosten":
            x.neben = num((v or "").replace("€", ""))
        elif kl == "heizkosten":
            x.heiz = num((v or "").replace("€", ""))
        elif "kaution" in kl:
            x.deposit_text = v

    tags = data.get("tags") or [clean(t.get_text(" ", strip=True)) for t in soup.select("#viewad-configuration li")]
    raw["tags"] = [t for t in tags if t]
    x.tags = " | ".join(filter(None, [x.title, attrs.get("Wohnungstyp"), *raw["tags"]]))

    # --- даты: formattedCreationDate — дата создания; дата на странице / sortingDate — дата
    # последнего «поднятия» (у поднятых старых объявлений она свежая)
    created = ddmmyyyy(data.get("formattedCreationDate"))
    extra = soup.select_one("#viewad-extra-info")
    shown = ddmmyyyy(extra.get_text(" ", strip=True)) if extra else None
    x.published = created or shown
    if data.get("sortingDate"):
        raw["bumped_at"] = dt.datetime.fromtimestamp(data["sortingDate"] / 1000, dt.timezone.utc).isoformat()
    raw["date_shown"] = shown

    # --- WBS: чекбокс сайта отмечается при поиске (found_via); здесь — упоминание в тексте
    raw["wbs_in_text"] = bool(WBS_WORD.search(" ".join(filter(None, [x.title, x.description]))))
    if any((a or "").lower() == "wbs" for a in raw["tags"]) or (attrs.get("WBS") or "").lower() in ("ja", "erforderlich"):
        x.wbs, x.wbs_source = "да", "метка на сайте"

    parse_seller(soup, data, x)
    raw["ad_status"] = data.get("adStatus")
    x.extra["detail_raw"] = raw
    x.detail_loaded = True


def parse_seller(soup, data: dict, x: Listing) -> None:
    user = data.get("userDetails") if isinstance(data.get("userDetails"), dict) else {}
    store = data.get("store") if isinstance(data.get("store"), dict) else {}
    box = soup.select_one("#viewad-contact, #viewad-profile-box")
    box_t = box.get_text(" | ", strip=True) if box else ""

    if "commercial" in user:
        x.seller_type = "commercial" if user["commercial"] else "private"
    elif "Gewerblicher Nutzer" in box_t:
        x.seller_type = "commercial"
    elif "Privater Nutzer" in box_t:
        x.seller_type = "private"

    x.seller_name = clean(store.get("title")) or clean(user.get("contactName")) or x.seller_name
    if not x.seller_name and box is not None:
        # первая ссылка — кружок с инициалами («H»), имя — самый длинный текст ссылок;
        # у коммерческих ссылки может не быть — тогда строка перед «Gewerblicher Nutzer»
        names = [clean(a.get_text(" ", strip=True)) for a in box.select("a[href*='userId='], a[href^='/pro/']")]
        names = [n for n in names if n and len(n) > 2 and not NOT_A_NAME.search(n)]
        parts = box_t.split(" | ")
        for label in ("Privater Nutzer", "Gewerblicher Nutzer"):
            if not names and label in parts:
                names = [p for p in parts[:parts.index(label)] if len(p) > 2 and not NOT_A_NAME.search(p)]
        x.seller_name = max(names, key=len) if names else None

    imprint = html_text(data.get("imprint"))
    phone = clean(user.get("phoneNumber"))
    if not phone and imprint:
        m = PHONE_RE.search(imprint)
        phone = clean(m.group(1)) if m else None
    x.contact_phone = phone
    m = EMAIL_RE.search(imprint or "")
    x.contact_email = m.group(0) if m else None

    user_id = user.get("userId")
    if user_id is None and box is not None:
        a = box.select_one("a[href*='userId=']")
        m = re.search(r"userId=(\d+)", a["href"]) if a else None
        user_id = m.group(1) if m else None
    since = user.get("userSince")
    if not since:
        m = re.search(r"Aktiv seit\s*(\d{2}\.\d{2}\.\d{4})", box_t)
        since = m.group(1) if m else None

    form = data.get("dynamicContactForm") if isinstance(data.get("dynamicContactForm"), dict) else {}
    form_fields = [f.get("name") for f in form.get("fields") or [] if isinstance(f, dict)]
    has_form = data.get("contactPosterEnabled")
    if has_form is None:
        has_form = soup.select_one("#viewad-contact-modal-form, #viewad-contact-button-login") is not None or None
    x.contact = {k: v for k, v in {
        "user_id": str(user_id) if user_id is not None else None,
        "user_since": since,
        "biz_user_type": user.get("bizUserType") or None,
        "profile_url": BASE + store["storeUrl"] if store.get("storeUrl") else
                       (f"{BASE}/s-bestandsliste.html?userId={user_id}" if user_id else None),
        "website": store.get("externalUrl"),
        "store_ads": store.get("numAds") or None,
        "imprint": imprint,
        "form": {"available": has_form,
                 "login_required": data.get("loginRequiredForContactPoster"),
                 "fields": form_fields or None},
        "phone_login_required": data.get("loginRequiredForPhoneNumber"),
    }.items() if v is not None}
