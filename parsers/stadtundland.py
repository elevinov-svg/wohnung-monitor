"""Stadt und Land: JSON API (всё сразу, подробная страница не нужна)."""

from urllib.parse import quote

from models import Listing
from parsers.common import (MAX_PAGES, clean, json_of, normalize_id, num, page_lines, post, price_pairs, require,
                            value_after,
                            wbs_from_text, wbs_type)

COMPANY = "Stadt und Land"
URL = "https://d2396ha8oiavw0.cloudfront.net/sul-main/immoSearch"
META: dict = {}


def fetch() -> list[Listing]:
    uniq, offset, total = {}, 0, 0
    for _ in range(MAX_PAGES):
        data = json_of(post(URL, json={"offset": offset, "cat": "wohnung"}), "data", "count")
        require(isinstance(data["data"], list), "data не список")
        rows = data["data"]
        total = int(data.get("count") or 0)
        for x in parse_list(rows):
            uniq.setdefault(x.external_id, x)
        offset += len(rows)
        if not rows or offset >= total:
            break
    META.clear()
    META.update(site_count=total or None)
    if total and len(uniq) < total:
        print(f"[warn] Stadt und Land: собрано {len(uniq)} из {total}")
    return list(uniq.values())


def parse_detail(html: str, x: Listing) -> None:
    """Подробная страница (серверный рендер): те же цены, что в API, плюс этаж,
    «verfügbar ab» и тип объекта. Для pipeline не нужна (enrich нет), читается
    только в режиме сбора."""
    lines = page_lines(html)
    x.prices.update({f"{k} (страница)": v for k, v in price_pairs(lines).items()})
    x.kalt = num(value_after(lines, "Kaltmiete")) or x.kalt
    x.neben = num(value_after(lines, "Nebenkosten")) or x.neben
    x.heiz = num(value_after(lines, "Heizkosten")) or x.heiz
    objekttyp = value_after(lines, "Objekttyp")
    if objekttyp:
        x.tags = f"{x.tags} {objekttyp}".strip()


def parse_list(rows: list[dict]) -> list[Listing]:
    out = []
    for r in rows:
        a, d, c = r.get("address") or {}, r.get("details") or {}, r.get("costs") or {}
        oid = d.get("immoNumber")
        if not oid:
            continue
        neben = num(c.get("additionalCosts"))
        # «totalRent» без Nebenkosten (меблированные комнаты) — не Warmmiete, а просто цена
        warm = num(c.get("warmRent")) or (num(c.get("totalRent")) if neben is not None else None)
        headline = clean(r.get("headline"))
        street = " ".join(filter(None, [a.get("street"), a.get("house_number")]))
        city = " ".join(filter(None, [a.get("postal_code"), a.get("city")]))
        out.append(Listing(
            # все ценовые поля API как есть: coldRent, warmRent, additionalCosts, heatingCosts,
            # totalRent (= Gesamtmiete с учётом скидки), discount (Mietnachlass), deposit
            prices={f"{k} (API)": v for k, v in c.items()},
            wbs_source="заголовок" if wbs_from_text(headline) else None,
            extra={"list_raw": r},
            company=COMPANY, external_id=normalize_id(oid),
            link="https://stadtundland.de/wohnungssuche/" + quote(oid, safe=""),
            title=headline, address=clean(f"{street}, {city}"), postcode=a.get("postal_code"),
            district=clean(a.get("precinct")), rooms=num(d.get("rooms")), area=num(d.get("livingSpace")),
            kalt=num(c.get("coldRent")), neben=neben, heiz=num(c.get("heatingCosts")), warm=warm,
            wbs=wbs_from_text(headline), wbs_type=wbs_type(headline),
            tags=" ".join(filter(None, [headline, d.get("immoType"), d.get("immoSubType")])),
            detail_loaded=True))
    return out
