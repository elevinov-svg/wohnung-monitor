"""
Google-таблица «Поиск квартиры Берлин», вкладка «Объявления».

Доступ — через сервисный аккаунт: GOOGLE_SERVICE_ACCOUNT_JSON (содержимое ключа)
или GOOGLE_SERVICE_ACCOUNT_FILE (путь к файлу ключа, удобно локально).

Колонки (порядок не менять):
ID, Дата обнаружения, Источник, Ссылка, Район, Адрес, Тип жилья, Комнаты,
Площадь (м2), Kaltmiete, Nebenkosten, Heizkosten, Warmmiete, WBS, Jobcenter,
До Ostkreuz, Контакт, Приоритет, Статус, Комментарий AI, Дата обновления
"""

import json
import os
from dataclasses import dataclass

SPREADSHEET_ID = "1rpcX6ViQtSUcRiGYvAKK-Kb703rwCsrjNmr_CmgJNKs"
TAB = "Объявления"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DATE_FMT = "%Y-%m-%d %H:%M"


@dataclass
class SheetRow:
    id: str
    found: str      # «Дата обнаружения», строка вида '2026-09-26 12:34'
    link: str


class Sheet:
    def __init__(self):
        self._ws = None

    def _worksheet(self):
        if self._ws is None:
            import gspread
            from google.oauth2.service_account import Credentials
            raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
            path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
            if raw:
                info = json.loads(raw)
            elif path:
                with open(path, encoding="utf-8") as f:
                    info = json.load(f)
            else:
                raise RuntimeError("Нужна переменная GOOGLE_SERVICE_ACCOUNT_JSON или GOOGLE_SERVICE_ACCOUNT_FILE")
            client = gspread.authorize(Credentials.from_service_account_info(info, scopes=SCOPES))
            self._ws = client.open_by_key(SPREADSHEET_ID).worksheet(TAB)
        return self._ws

    def existing(self) -> list[SheetRow]:
        """ID, дата обнаружения и ссылка всех строк — для проверки дублей перед записью."""
        out = []
        for v in self._worksheet().get("A2:D"):
            v = [str(c).strip() for c in v] + [""] * 4
            out.append(SheetRow(v[0], v[1], v[3]))
        return out

    def append(self, rows: list[list]) -> None:
        if rows:
            self._worksheet().append_rows(rows, value_input_option="USER_ENTERED")
