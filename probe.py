#!/usr/bin/env python3
"""Разведка №2: пагинация HOWOGE, degewo, GESOBAU."""
import json
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

OUT = Path("probe_out")
OUT.mkdir(exist_ok=True)
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36", "Accept-Language": "de-DE,de;q=0.9"})
res = {}

# --- HOWOGE: разные варианты параметров
HU = "https://www.howoge.de/?type=999&tx_howrealestate_json_list[action]=immoList"
def hw(page, limit, rooms="", wbs=""):
    form = {"tx_howrealestate_json_list[page]": str(page), "tx_howrealestate_json_list[limit]": str(limit),
            "tx_howrealestate_json_list[lang]": "", "tx_howrealestate_json_list[rooms]": rooms,
            "tx_howrealestate_json_list[wbs]": wbs}
    d = S.post(HU, data=form, timeout=30).json()
    return {"count": d.get("immocount"), "n": len(d.get("immoobjects") or []),
            "uids": [o["uid"] for o in d.get("immoobjects") or []][:5],
            "rooms": sorted({o["rooms"] for o in d.get("immoobjects") or []})}
res["howoge"] = {}
for args in [(1, 12), (2, 12), (3, 12), (12, 12), (1, 12, "1"), (1, 12, "2"), (2, 12, "2"), (1, 50, "2"), (2, 30)]:
    try:
        res["howoge"][str(args)] = hw(*args)
    except Exception as e:  # noqa
        res["howoge"][str(args)] = repr(e)

# --- degewo: обход страниц, логируем ссылки
DU = "https://www.degewo.de/immosuche"
log, todo, done, uids = [], [DU], set(), []
while todo and len(done) < 15:
    u = todo.pop(0)
    if u in done:
        continue
    done.add(u)
    soup = BeautifulSoup(S.get(u, timeout=30).text, "html.parser")
    teasers = soup.select(".c-teaser__inner")
    ids = [b["data-openimmo-bookmark-item-uid"] for b in soup.select("[data-openimmo-bookmark-item-uid]")]
    uids += ids
    links = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"tx_openimmo_immobilie%5Bpage%5D=(\d+)", a["href"])
        if m:
            links[int(m.group(1))] = a["href"].split("#")[0]
    rc = soup.select_one(".results-count")
    log.append({"url": u[-80:], "teasers": len(teasers), "ids": len(ids), "links": sorted(links),
                "count": rc.get_text(strip=True) if rc else None})
    for n in sorted(links):
        full = requests.compat.urljoin(DU, links[n])
        if full not in done and full not in todo:
            todo.append(full)
res["degewo"] = {"log": log, "unique_ids": len(set(uids))}
# вариант: GET-параметр страницы без cHash
try:
    t = S.get(DU + "?tx_openimmo_immobilie%5Bpage%5D=6", timeout=30)
    res["degewo"]["nochash_page6"] = [t.status_code, len(re.findall("data-openimmo-bookmark-item-uid", t.text))]
except Exception as e:  # noqa
    res["degewo"]["nochash_page6"] = repr(e)

# --- GESOBAU: страницы 1..6
GU = "https://www.gesobau.de/mieten/wohnungssuche/"
res["gesobau"] = []
for p in range(1, 7):
    u = GU if p == 1 else f"{GU}?tx_solr%5Bpage%5D={p}"
    soup = BeautifulSoup(S.get(u, timeout=30).text, "html.parser")
    ents = soup.select(".results-entry a[href*='detailseite']")
    txt = soup.get_text(" ", strip=True)
    m = re.search(r"(\d+)\s+(?:Ergebnisse|Wohnungen|Treffer|Angebote)", txt)
    res["gesobau"].append({"page": p, "entries": len({a['href'] for a in ents}),
                           "first": ents[0]["href"][-60:] if ents else None, "count_text": m.group(0) if m else None,
                           "has_wilhelmsruher118": "wilhelmsruher-damm-10-00911" in soup.decode()})
    if p == 1:
        (OUT / "gesobau_p1.html").write_text(soup.decode(), encoding="utf-8")
# ссылка из портала
try:
    r = S.get("https://www.gesobau.de/?immo_ref=10-00911-00010-0774-693f9eb9-db4b-42fa-a788-ad0e2aa881c9",
              timeout=30, allow_redirects=True)
    res["gesobau_deeplink"] = [r.status_code, r.url]
except Exception as e:  # noqa
    res["gesobau_deeplink"] = repr(e)

(OUT / "probe2.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps(res, ensure_ascii=False)[:3000])
