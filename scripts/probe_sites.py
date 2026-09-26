#!/usr/bin/env python3
"""Как сайты отвечают с этого IP: статус, размер, заголовки CDN/защиты, признаки капчи.
Для замера из GitHub Actions (Cloudflare у Gewobag, cookie-редиректы у HOWOGE)."""

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parsers.common import new_session  # noqa: E402

TARGETS = [
    ("HOWOGE", "POST", "https://www.howoge.de/?type=999&tx_howrealestate_json_list[action]=immoList",
     {"data": {"tx_howrealestate_json_list[page]": "1"}}),
    ("degewo", "GET", "https://www.degewo.de/immosuche", {}),
    ("WBM", "GET", "https://www.wbm.de/wohnungen-berlin/angebote/", {}),
    ("GESOBAU", "GET", "https://www.gesobau.de/mieten/wohnungssuche/", {}),
    ("Gewobag", "GET", "https://www.gewobag.de/fuer-mietinteressentinnen/mietangebote/?objekttyp%5B%5D=wohnung&sort-by=recent", {}),
    ("Stadt und Land", "POST", "https://d2396ha8oiavw0.cloudfront.net/sul-main/immoSearch",
     {"json": {"offset": 0, "cat": "wohnung"}}),
]
HEADERS = ("server", "cf-ray", "cf-cache-status", "age", "x-cache", "cache-control", "set-cookie")
CAPTCHA = re.compile(r"cf-chl|challenge-form|just a moment|captcha|access denied|attention required", re.I)


def main() -> int:
    s = new_session()
    ip = ""
    try:
        ip = s.get("https://api.ipify.org", timeout=10).text
    except Exception:  # noqa: BLE001
        pass
    print(f"IP: {ip}")
    for name, method, url, kw in TARGETS:
        t = time.monotonic()
        try:
            r = s.request(method, url, timeout=30, **kw)
            hist = [h.status_code for h in r.history]
            hdr = {k: (r.headers.get(k) or "")[:60] for k in HEADERS if r.headers.get(k)}
            cap = bool(CAPTCHA.search(r.text[:20000]))
            print(f"{name}: HTTP {r.status_code} редиректы {hist} {len(r.text)} байт "
                  f"{time.monotonic() - t:.1f}с капча={cap} {hdr}")
        except Exception as exc:  # noqa: BLE001
            print(f"{name}: ОШИБКА {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
