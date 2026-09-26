"""Общие утилиты парсеров: HTTP, разбор чисел, ID, WBS."""

import random
import re
import threading
import time

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
TIMEOUT = 25
RETRIES = 2                 # повторов при таймауте / обрыве соединения / 502-504
RETRY_PAUSE = (10, 20)      # пауза перед повтором, сек (случайная в диапазоне)
RETRY_STATUS = {502, 503, 504}
MAX_PAGES = 20
DETAIL_DELAY = 0.5   # пауза перед каждой подробной страницей, сек

_local = threading.local()


class StructureError(Exception):
    """Ответ пришёл, но в нём нет ожидаемой структуры (контейнера результатов,
    ключей JSON, формы поиска). Это поломка парсера/сайта, а не «0 объявлений»."""


def stats() -> dict:
    """Счётчики HTTP текущего потока (у каждой компании свой поток)."""
    if not hasattr(_local, "stats"):
        _local.stats = {"requests": 0, "errors": 0, "retries": 0}
    return _local.stats


def count_response(r, *args, **kwargs):
    st = stats()
    st["requests"] += 1
    if r.status_code >= 400:
        st["errors"] += 1


class RetrySession(requests.Session):
    """Таймаут по умолчанию и до RETRIES повторов с паузой 10–20 с при таймауте,
    ошибке соединения или 502/503/504."""

    def request(self, method, url, **kw):
        kw.setdefault("timeout", TIMEOUT)
        for attempt in range(RETRIES + 1):
            try:
                r = super().request(method, url, **kw)
                if r.status_code not in RETRY_STATUS or attempt == RETRIES:
                    return r
                why = f"HTTP {r.status_code}"
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == RETRIES:
                    raise
                why = type(exc).__name__
            stats()["retries"] += 1
            pause = random.uniform(*RETRY_PAUSE)
            print(f"[retry] {why} {url[:100]} — повтор {attempt + 1}/{RETRIES} через {pause:.0f} с", flush=True)
            time.sleep(pause)


def new_session() -> requests.Session:
    s = RetrySession()
    s.headers.update({"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9"})
    s.hooks["response"].append(count_response)
    return s


class _ThreadSession:
    """Общая сессия парсеров — своя в каждом потоке (компании собираются параллельно)."""

    def __getattr__(self, name):
        if not hasattr(_local, "session"):
            _local.session = new_session()
        return getattr(_local.session, name)


session = _ThreadSession()


def get(url: str, **kw) -> requests.Response:
    r = session.get(url, **kw)
    r.raise_for_status()
    return r


def post(url: str, **kw) -> requests.Response:
    r = session.post(url, **kw)
    r.raise_for_status()
    return r


def get_detail(url: str) -> str:
    time.sleep(DETAIL_DELAY)
    return get(url).text


def require(cond, what: str) -> None:
    if not cond:
        raise StructureError(what)


def json_of(r: requests.Response, *keys: str) -> dict:
    """JSON-объект ответа с обязательными ключами, иначе StructureError."""
    try:
        data = r.json()
    except ValueError as exc:
        raise StructureError(f"ответ не JSON ({r.headers.get('content-type')})") from exc
    require(isinstance(data, dict), f"JSON не объект: {type(data).__name__}")
    missing = [k for k in keys if k not in data]
    require(not missing, f"в JSON нет ключей {missing}")
    return data


def num(text) -> float | None:
    """'1.423,09 €' -> 1423.09 ; '45,52' -> 45.52 ; 'ca. 75.91 m²' -> 75.91 ; 'unbekannt' -> None"""
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"\d[\d.]*(?:,\d+)?", str(text))
    if not m:
        return None
    s = m.group(0).rstrip(".")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")  # '1.498' = тысяча
    try:
        return float(s)
    except ValueError:
        return None


def normalize_id(raw) -> str:
    """ID объекта в родном виде: без пробелов, верхний регистр. Разделители сохраняем,
    чтобы '1770-2403-555' и '1770-24035-55' не слились в один ключ."""
    return re.sub(r"\s+", "", str(raw or "")).upper()


def clean(text) -> str | None:
    if text is None:
        return None
    t = re.sub(r"\s+", " ", str(text)).strip()
    return t or None


PLZ_RE = re.compile(r"\b(\d{5})\b")


def plz_of(text: str | None) -> str | None:
    found = PLZ_RE.findall(text or "")
    return found[-1] if found else None


WBS_NO = re.compile(r"ohne\s+wbs|kein(?:en)?\s+wbs|wbs\s+(?:ist\s+)?nicht\s+(?:erforderlich|nötig|notwendig)"
                    r"|wbs\s*(?:erforderlich)?\s*:\s*nein|freifinanziert", re.I)
WBS_YES = re.compile(r"(?:wohnberechtigungsschein|\bwbs\b)[^.]{0,80}?(?:benötigt|erforderlich|pflicht|notwendig|voraussetzung)"
                     r"|nur\s+mit\s+wbs|\bmit\s+wbs\b|wbs[- ]pflicht|\bwbs[- ]?\d{3}", re.I)
# строки меню/подвала, где «WBS» есть на каждой странице сайта
WBS_BOILERPLATE = re.compile(r"schnell-?check|wbs-rechner|^wohnberechtigungsschein \(wbs\)$|^\* wohnberechtigungsschein\.?$"
                             r"|^wbs$|^alles zum wohnberechtigungsschein", re.I)


def wbs_type(text: str | None) -> str | None:
    """Тип WBS из текста: 'WBS 160, 180 oder 220' -> '160/180/220'; + besonderer Wohnbedarf."""
    t = text or ""
    nums = []
    for m in re.finditer(r"(?:wbs|wohnberechtigungsschein)[^.]{0,40}", t, re.I):
        nums += re.findall(r"\b(100|140|160|180|220)\b", m.group(0))
    kinds = sorted(set(nums), key=int)
    out = "/".join(kinds)
    if re.search(r"besonder\w*\s+wohnbedarf", t, re.I):
        out = (out + " + " if out else "") + "besonderer Wohnbedarf"
    return out or None


def wbs_in_description(lines: list[str]) -> tuple[str | None, str | None]:
    """(«да»/«нет»/None, тип) по тексту описания подробной страницы, без меню и подвала."""
    text = " ".join(ln for ln in lines if not WBS_BOILERPLATE.search(ln.strip()))
    if WBS_NO.search(text):
        return "нет", None
    if WBS_YES.search(text):
        hits = " ".join(m.group(0) + text[m.end():m.end() + 60] for m in WBS_YES.finditer(text))
        return "да", wbs_type(hits)
    return None, None


def wbs_from_text(text: str | None) -> str | None:
    """Только для конкретных полей (заголовок, метки, «WBS erforderlich: …»),
    не для всей страницы — в меню сайтов везде есть слово «WBS»."""
    t = (text or "").lower()
    if re.search(r"ohne\s+wbs|kein(?:en)?\s+wbs|wbs\s+(?:ist\s+)?nicht\s+erforderlich"
                 r"|wbs\s*(?:erforderlich)?\s*:\s*nein|freifinanziert", t):
        return "нет"
    if re.search(r"\bwbs\b|wohnberechtigungsschein", t):
        return "да"
    return None


def page_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return [ln.strip() for ln in soup.get_text("\n").split("\n") if ln.strip()]


PRICE_LABEL = re.compile(r"miete|kosten|kaution|nachlass|zuschlag|pauschal|vorauszahlung|^vz\b", re.I)


MONEY = re.compile(r"^(?:ab\s+)?-?\s*\d[\d.,\s]*(?:€|EUR|Euro)?$|€|\bEUR\b|\bEuro\b", re.I)


def price_pairs(lines: list[str]) -> dict:
    """Все ценовые пары со страницы как есть: «Kaltmiete» -> «825,00 €».
    Метка — короткая строка со словом Miete/Kosten/Kaution/Nachlass…, значение —
    следующая строка (или остаток строки после двоеточия), похожая на сумму;
    для Kaution — любой короткий текст («drei Nettokaltmieten»)."""
    out = {}
    for i, ln in enumerate(lines):
        m = re.match(r"^([^:]{2,45}):\s*(\S.{0,60})$", ln)
        if m and PRICE_LABEL.search(m.group(1)) and MONEY.search(m.group(2).strip()):
            out.setdefault(m.group(1).strip(), re.sub(r"\s+", " ", m.group(2)).strip())
            continue
        label = ln.rstrip(":").strip()
        if len(label) > 45 or not PRICE_LABEL.search(label) or i + 1 >= len(lines):
            continue
        if re.match(r"(?i)euro\s", label):
            # degewo: «487,01 / Euro Warmmiete» — сумма стоит ПЕРЕД меткой
            if i and MONEY.search(lines[i - 1]):
                out.setdefault(label.split(None, 1)[1], f"{lines[i - 1]} €")
            continue
        val = re.sub(r"\s+", " ", lines[i + 1]).strip()
        if len(val) > 60 or val.endswith(":"):
            continue
        if MONEY.search(val) or (label.lower().startswith("kaution") and not PRICE_LABEL.fullmatch(val)):
            out.setdefault(label, val)
    return out


def value_after(lines: list[str], *labels: str) -> str | None:
    """Значение из пары «Метка / значение на следующей строке». Метка сравнивается
    целиком (без двоеточия и регистра), чтобы не цеплять слова из меню и текста."""
    wanted = {lb.lower() for lb in labels}
    for i, ln in enumerate(lines[:-1]):
        if ln.rstrip(":").strip().lower() in wanted:
            return lines[i + 1]
    return None


def coords_from_html(html: str) -> tuple[float, float] | None:
    m = re.search(r'data-(?:location-)?lat="(5[23]\.\d+)"\s+data-(?:location-)?lng="(1[34]\.\d+)"', html)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


# ----------------------------------------------------------------- поля подробной страницы

FLOOR_KEYS = re.compile(r"^(etage|geschoss|stockwerk|lage im haus)$", re.I)
AVAIL_KEYS = re.compile(r"^(frei ab|bezugsfrei ab|bezugsfrei|verfügbar ab|bezugsfertig ab|bezug ab)$", re.I)
DEPOSIT_KEY = re.compile(r"kaution|deposit", re.I)


def detail_pairs(html: str) -> dict:
    """Все пары «метка → значение» со страницы: dt/dd, th/td, «Метка:» + следующая строка,
    «Метка: значение» в одной строке. Сохраняются в raw."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        tag.decompose()
    pairs = {}

    def put(k, v):
        k = re.sub(r"\s+", " ", k or "").strip().rstrip(":").strip()
        v = re.sub(r"\s+", " ", v or "").strip()
        if k and v and len(k) <= 40 and len(v) <= 200 and k not in pairs:
            pairs[k] = v

    for dl in soup.find_all("dl"):
        for dt_, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
            put(dt_.get_text(" "), dd.get_text(" "))
    for tr in soup.find_all("tr"):
        th, td = tr.find("th"), tr.find("td")
        if th and td:
            put(th.get_text(" "), td.get_text(" "))
    lines = [ln.strip() for ln in soup.get_text("\n").split("\n") if ln.strip()]
    for i, ln in enumerate(lines):
        m = re.match(r"^([A-ZÄÖÜ][\wäöüß .()/-]{1,38}):\s*(\S.*)$", ln)
        if m:
            put(m.group(1), m.group(2))
        elif ln.endswith(":") and i + 1 < len(lines) and len(ln) <= 41:
            put(ln, lines[i + 1])
    return pairs


def detail_extras(html: str, x, pairs: dict) -> None:
    """Этаж, «доступна с», дата публикации (JSON-LD, Gewobag), координаты — в поля модели.
    Не затирает то, что парсер уже заполнил."""
    lines = page_lines(html)
    text = " ".join(pairs.values()) + " " + " ".join(lines)
    def after(*labels):
        # как value_after, но пропускает «значения» без букв и цифр (degewo: «frei ab» / «(»)
        wanted = {lb.lower() for lb in labels}
        return next((lines[i + 1] for i, ln in enumerate(lines[:-1])
                     if ln.rstrip(":").strip().lower() in wanted and re.search(r"\w", lines[i + 1])), None)

    floor = after("Etage", "Geschoss", "Stockwerk", "Lage im Haus")
    avail = after("frei ab", "Bezugsfrei ab", "Bezugsfrei", "verfügbar ab", "Bezugsfertig ab")
    for k, v in pairs.items():
        floor = floor or (v if FLOOR_KEYS.match(k) else None)
        avail = avail or (v if AVAIL_KEYS.match(k) else None)
    if not floor:
        m = re.search(r"\b(\d{1,2})\.\s*(?:Etage|OG|Obergeschoss)\b|\b(Erdgeschoss|Dachgeschoss|Souterrain)\b", text)
        floor = m.group(0) if m else None
    if not avail:
        m = re.search(r"(?:Frei|Bezugsfrei|Verfügbar) ab:?\s*(\d{2}\.\d{2}\.\d{4}|sofort)", text, re.I)
        avail = m.group(1) if m else None
    x.floor = x.floor or clean(floor)
    x.available_from = x.available_from or clean(avail)
    m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html)
    if m and not x.published:
        x.published = m.group(1)
    c = coords_from_html(html)
    if c and x.lat is None:
        x.lat, x.lon = c


def fill_deposit(x) -> None:
    """Kaution из ценовых полей: число, если это сумма («1.234,00 €»), иначе текст
    («drei Nettokaltmieten»)."""
    for k, v in (x.prices or {}).items():
        if DEPOSIT_KEY.search(k) and v not in (None, ""):
            s = str(v).strip()
            x.deposit_text = s
            money = re.search(r"\d[\d.,]*\s*(?:€|EUR|Euro)", s, re.I) or re.fullmatch(r"\d[\d.,]*", s)
            x.deposit = num(money.group(0)) if money else None
            return
