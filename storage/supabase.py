"""
Supabase через REST (PostgREST), без лишних зависимостей.

Таблицы:
  listings       — прошедшие фильтр объявления (upsert по source+external_id)
  seen_listings  — ВСЕ увиденные объявления, решение фильтра и флаги записи
  parser_runs    — журнал: одна строка на компанию за цикл
  parser_health  — текущее состояние парсера (ok/alert) и счётчик неудач подряд
  geocache       — кэш геокодера

SUPABASE_KEY — секретный ключ (sb_secret_... / service_role). RLS включён без
политик, поэтому писать может только этот ключ.
"""

import os

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://vbkrmzjmmbhtnqjotrnz.supabase.co").rstrip("/")
PAGE = 1000

SEEN_COLS = ("company,external_id,link,first_seen,last_seen,current_since,passed_filter,"
             "reject_reason,written_sheet,written_db,legacy")


class SupabaseError(Exception):
    pass


class Supabase:
    def __init__(self, key: str | None = None, url: str = SUPABASE_URL):
        self.key = key if key is not None else (os.environ.get("SUPABASE_KEY") or "")
        self.url = url
        self.http = requests.Session()
        self.http.headers.update({"apikey": self.key, "Authorization": f"Bearer {self.key}",
                                  "Content-Type": "application/json"})

    @property
    def configured(self) -> bool:
        return bool(self.key)

    # ------------------------------------------------------------ низкий уровень

    def _req(self, method: str, table: str, params=None, json=None, prefer=None, headers=None):
        if not self.configured:
            raise SupabaseError("SUPABASE_KEY не задан")
        h = dict(headers or {})
        if prefer:
            h["Prefer"] = prefer
        try:
            r = self.http.request(method, f"{self.url}/rest/v1/{table}", params=params, json=json,
                                  headers=h, timeout=30)
        except requests.RequestException as exc:
            raise SupabaseError(f"{table}: сеть: {exc}") from exc
        if not r.ok:
            raise SupabaseError(f"{table}: HTTP {r.status_code}: {r.text[:300]}")
        return r

    def select(self, table: str, params: dict) -> list[dict]:
        out, start = [], 0
        while True:
            r = self._req("GET", table, params=params,
                          headers={"Range-Unit": "items", "Range": f"{start}-{start + PAGE - 1}"})
            rows = r.json()
            out += rows
            if len(rows) < PAGE:
                return out
            start += PAGE

    def upsert(self, table: str, rows: list[dict], on_conflict: str) -> None:
        if not rows:
            return
        # PostgREST требует одинаковый набор ключей во всех строках пакета. Дополнять
        # недостающие ключи null нельзя — это затёрло бы существующие значения,
        # поэтому отправляем группами с одинаковым набором ключей.
        groups: dict[tuple, list] = {}
        for r in rows:
            groups.setdefault(tuple(sorted(r)), []).append(r)
        for group in groups.values():
            for i in range(0, len(group), 500):
                self._req("POST", table, params={"on_conflict": on_conflict}, json=group[i:i + 500],
                          prefer="resolution=merge-duplicates,return=minimal")

    def insert(self, table: str, rows: list[dict]) -> None:
        if rows:
            self._req("POST", table, json=rows, prefer="return=minimal")

    def delete(self, table: str, params: dict) -> None:
        self._req("DELETE", table, params=params, prefer="return=minimal")

    def rpc(self, fn: str, args: dict):
        return self._req("POST", f"rpc/{fn}", json=args).json()

    # ------------------------------------------------------------ seen_listings

    def load_seen(self, company: str) -> dict[str, dict]:
        rows = self.select("seen_listings", {"select": SEEN_COLS, "company": f"eq.{company}"})
        return {r["external_id"]: r for r in rows}

    def load_unfinished(self, company: str) -> list[dict]:
        """Не до конца обработанные: решение не принято (повтор геокодера)
        или прошли фильтр, но не записаны в одно из хранилищ. С raw."""
        return self.select("seen_listings", {
            "select": SEEN_COLS + ",raw", "company": f"eq.{company}",
            "or": "(passed_filter.is.null,and(passed_filter.is.true,"
                  "or(written_sheet.is.false,written_db.is.false)))"})

    def save_seen(self, rows: list[dict]) -> None:
        self.upsert("seen_listings", rows, "company,external_id")

    # ------------------------------------------------------------ listings

    def upsert_listings(self, rows: list[dict]) -> None:
        self.upsert("listings", rows, "source,external_id")

    # ------------------------------------------------------------ журнал и здоровье

    def insert_runs(self, rows: list[dict]) -> None:
        self.insert("parser_runs", rows)

    def prune_runs(self, before_iso: str) -> None:
        self.delete("parser_runs", {"started_at": f"lt.{before_iso}"})

    def load_health(self) -> dict[str, dict]:
        return {r["company"]: r for r in self.select("parser_health", {"select": "*"})}

    def save_health(self, rows: list[dict]) -> None:
        self.upsert("parser_health", rows, "company")

    # ------------------------------------------------------------ geocache

    def load_geocache(self) -> dict:
        rows = self.select("geocache", {"select": "query,lat,lon"})
        return {r["query"]: ((r["lat"], r["lon"]) if r["lat"] is not None else None) for r in rows}

    def save_geocache(self, entries: dict) -> None:
        self.upsert("geocache", [{"query": q, "lat": c[0] if c else None, "lon": c[1] if c else None}
                                 for q, c in entries.items()], "query")

