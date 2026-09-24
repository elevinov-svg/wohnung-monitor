#!/usr/bin/env python3
"""
Разведка: скачивает страницы/эндпоинты жилищных компаний и сохраняет
сырые ответы в probe_out/, чтобы по ним написать парсеры.
Запускается workflow .github/workflows/probe.yml в ветке probe.
"""
import json
import re
import time
from pathlib import Path

import requests

OUT = Path("probe_out")
(OUT / "raw").mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
H = {"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9"}

HOWOGE_FORM = {
    "tx_howrealestate_json_list[page]": "1",
    "tx_howrealestate_json_list[limit]": "100",
    "tx_howrealestate_json_list[lang]": "",
    "tx_howrealestate_json_list[rent]": "",
    "tx_howrealestate_json_list[area]": "",
    "tx_howrealestate_json_list[rooms]": "egal",
    "tx_howrealestate_json_list[wbs]": "all-offers",
}

TARGETS = [
    ("inberlinwohnen_finder", "GET", "https://www.inberlinwohnen.de/wohnungsfinder/", None, None),
    ("degewo_search_json", "GET",
     "https://immosuche.degewo.de/de/search.json?utf8=%E2%9C%93&property_type_id=1"
     "&categories%5B%5D=1&order=rent_total_without_vat_asc&per_page=100", None, None),
    ("degewo_immosuche_html", "GET", "https://www.degewo.de/immosuche", None, None),
    ("howoge_json", "POST",
     "https://www.howoge.de/?type=999&tx_howrealestate_json_list[action]=immoList", HOWOGE_FORM, None),
    ("howoge_html", "GET", "https://www.howoge.de/immobiliensuche/wohnungssuche.html", None, None),
    ("wbm_html", "GET", "https://www.wbm.de/wohnungen-berlin/angebote/", None, None),
    ("gesobau_html", "GET", "https://www.gesobau.de/mieten/wohnungssuche/?resultsPerPage=100", None, None),
    ("gewobag_html", "GET",
     "https://www.gewobag.de/fuer-mietinteressentinnen/mietangebote/?objekttyp%5B%5D=wohnung&sort-by=recent",
     None, None),
    ("stadtundland_api", "POST", "https://d2396ha8oiavw0.cloudfront.net/sul-main/immoSearch",
     None, {"offset": 0, "cat": "wohnung"}),
    ("stadtundland_html", "GET", "https://www.stadtundland.de/wohnungssuche", None, None),
]

summary = []
for name, method, url, form, js in TARGETS:
    rec = {"name": name, "method": method, "url": url}
    t0 = time.time()
    try:
        if method == "GET":
            r = requests.get(url, headers=H, timeout=40)
        else:
            r = requests.post(url, headers=H, data=form, json=js, timeout=40)
        rec.update({
            "status": r.status_code,
            "final_url": r.url,
            "content_type": r.headers.get("content-type", ""),
            "bytes": len(r.content),
            "seconds": round(time.time() - t0, 2),
        })
        ext = "json" if "json" in rec["content_type"] else "html"
        (OUT / "raw" / f"{name}.{ext}").write_bytes(r.content)
        text = r.text
        # немного подсказок для анализа
        rec["title"] = (re.search(r"<title>(.*?)</title>", text, re.S) or [None, ""])[1].strip()[:150]
        rec["json_hint"] = sorted(set(re.findall(r"https?://[^\"' ]*(?:api|json|ajax|search)[^\"' ]*", text)))[:25]
        rec["has_livewire"] = "livewire" in text.lower()
        rec["has_next_data"] = "__NEXT_DATA__" in text or "__NUXT__" in text
    except Exception as exc:  # noqa: BLE001
        rec["error"] = repr(exc)
    summary.append(rec)
    print(json.dumps(rec, ensure_ascii=False)[:400])
    time.sleep(1)

(OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
