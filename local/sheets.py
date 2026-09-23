"""
Запись новых объявлений во вкладку "Объявления" Google-таблицы проекта.

Использует сервисный аккаунт Google (не твой личный логин/пароль —
отдельная техническая учётная запись, у которой есть доступ только к
этой конкретной таблице, потому что мы её туда добавим вручную).

Настройка (один раз, см. README.md, раздел "Google Sheets — настройка"):
  1. Создать сервисный аккаунт в Google Cloud Console, включить Sheets API.
  2. Скачать JSON-ключ сервисного аккаунта.
  3. Открыть таблицу → "Настройки доступа" → добавить email сервисного
     аккаунта (вида xxx@yyy.iam.gserviceaccount.com) с правом "Редактор".
  4. Передать содержимое JSON-ключа скрипту через переменную окружения
     GOOGLE_SERVICE_ACCOUNT_JSON (в GitHub Actions — через Secret;
     локально — через файл, см. README-local.md).
"""

import json
import os

import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = "1rpcX6ViQtSUcRiGYvAKK-Kb703rwCsrjNmr_CmgJNKs"
SHEET_TAB_NAME = "Объявления"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_client_cache = None


def _get_worksheet():
    global _client_cache
    if _client_cache is None:
        # Два варианта, чтобы было удобно и в GitHub Secrets (содержимое
        # целиком в переменной), и локально на своей машине (проще
        # указать путь к файлу, чем пихать длинный JSON в переменную
        # окружения, особенно на Windows).
        creds_raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")

        if creds_raw:
            creds_info = json.loads(creds_raw)
        elif creds_path:
            creds_info = json.loads(open(creds_path, encoding="utf-8").read())
        else:
            raise RuntimeError(
                "Нужна переменная окружения GOOGLE_SERVICE_ACCOUNT_JSON "
                "(содержимое JSON-ключа) или GOOGLE_SERVICE_ACCOUNT_FILE "
                "(путь к файлу с ключом). См. README.md, раздел про Google Sheets."
            )
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
        _client_cache = gspread.authorize(creds)

    spreadsheet = _client_cache.open_by_key(SPREADSHEET_ID)
    return spreadsheet.worksheet(SHEET_TAB_NAME)


def append_rows(rows: list[list]) -> None:
    """Добавляет одну или несколько строк в конец вкладки "Объявления"."""
    if not rows:
        return
    worksheet = _get_worksheet()
    worksheet.append_rows(rows, value_input_option="USER_ENTERED")
