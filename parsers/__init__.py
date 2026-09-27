"""Парсеры шести городских жилищных компаний Берлина.

У каждого модуля:
  COMPANY          — название компании (оно же «Источник» в таблице)
  fetch()          — список Listing со страницы/API списка
  enrich(listing)  — (не у всех) дочитать подробную страницу: Kalt/Neben/Heiz, WBS, индекс
  parse_*()        — чистые функции разбора, покрыты тестами на образцах из tests/fixtures
"""

from parsers import degewo, gesobau, gewobag, howoge, stadtundland, wbm

PARSERS = {m.COMPANY: m for m in (howoge, degewo, wbm, gesobau, gewobag, stadtundland)}
