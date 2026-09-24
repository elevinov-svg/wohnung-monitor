#!/usr/bin/env python3
"""Разведка №3: работает ли фильтр по району у HOWOGE."""
import json
from pathlib import Path
import requests

OUT = Path("probe_out"); OUT.mkdir(exist_ok=True)
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"})
HU = "https://www.howoge.de/?type=999&tx_howrealestate_json_list[action]=immoList"
KIEZE = ["", "Brandenburg", "Charlottenburg-Wilmersdorf", "Friedrichshain-Kreuzberg", "Marzahn-Hellersdorf", "Mitte",
         "Neukölln", "Lichtenberg", "Pankow", "Reinickendorf", "Spandau", "Steglitz-Zehlendorf",
         "Tempelhof-Schöneberg", "Treptow-Köpenick"]
res = {}
for k in KIEZE:
    for variant in ("kiez[]", "kiez"):
        form = {"tx_howrealestate_json_list[page]": "1", "tx_howrealestate_json_list[limit]": "12",
                "tx_howrealestate_json_list[lang]": "", "tx_howrealestate_json_list[rooms]": "",
                "tx_howrealestate_json_list[wbs]": ""}
        if k:
            form[f"tx_howrealestate_json_list[{variant}]"] = k
        d = S.post(HU, data=form, timeout=30).json()
        objs = d.get("immoobjects") or []
        res[f"{k or 'ALL'} | {variant}"] = {"count": d.get("immocount"), "n": len(objs),
                                            "districts": sorted({o.get("district") for o in objs})}
        if not k:
            break
(OUT / "probe3.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps(res, ensure_ascii=False, indent=1))
