"""Общие утилиты парсеров: HTTP, разбор чисел, ID, WBS."""

import re
import time

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
TIMEOUT = 30
MAX_PAGES = 20
DETAIL_DELAY = 0.5   # пауза перед каждой подробной страницей, сек

HTTP = {"requests": 0, "errors": 0}   # счётчик для замеров (collect.py)


def count_response(r, *args, **kwargs):
    HTTP["requests"] += 1
    if r.status_code >= 400:
        HTTP["errors"] += 1


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9"})
    s.hooks["response"].append(count_response)
    return s


session = new_session()


def get(url: str, **kw) -> requests.Response:
    r = session.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def post(url: str, **kw) -> requests.Response:
    r = session.post(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def get_detail(url: str) -> str:
    time.sleep(DETAIL_DELAY)
    return get(url).text


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
