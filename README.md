# Wohnung-Monitor

Следит за сайтами шести городских жилищных компаний Берлина — HOWOGE, degewo,
WBM, GESOBAU, Gewobag, Stadt und Land — и записывает подходящие квартиры
**одновременно** в Google-таблицу «Поиск квартиры Берлин» (вкладка «Объявления»)
и в базу Supabase (`public.listings`).

Главный принцип: лучше лишняя строка, чем пропущенная квартира. Если поле
неизвестно — по нему не отсеиваем.

## Один процесс, два результата

Workflow «Сбор объявлений» (`.github/workflows/collect.yml`, `collect.py`) каждые
30 минут, ~50 минут, проверка каждые 3 минуты. Каждый цикл компании:

1. **сбор** — все объявления без фильтров в Supabase (таблицы ниже);
2. **фильтр** — новые из того же списка (сайт второй раз не опрашивается) проходят
   фильтр, подходящие дописываются в Google-таблицу и в `listings` (см. «Как работает фильтр»).

Ошибка фильтра или таблицы не мешает сбору. Сбор пишет:

| Таблица | Что в ней |
|---|---|
| `collected_listings` | все объявления: поля, `prices`, `raw`, WBS, `first_seen` / `last_seen` / `disappeared_at`, `baseline` (первый цикл) |
| `listing_changes` | изменения полей (было / стало / когда), `field = 'status'` — исчезло / вернулось |
| `parser_runs` | строка на компанию за цикл: статус, счётчики, повторы, ошибка |
| `parser_health` | `ok` / `suspect` / `alert` |

- Компании собираются параллельно, каждая в своём потоке.
- Запрос: таймаут 25 с, до 2 повторов через 10–20 с (таймаут, соединение, 502–504).
- Нет ожидаемой структуры (контейнер списка, ключи JSON, форма) — ошибка, а не «0».
  «0» при валидной структуре после непустого — `suspect`, исчезновения не отмечаются,
  перепроверка в следующем цикле.
- Подробная страница — сразу для новых, для известных раз в сутки.
- Gewobag: фильтры WBS обходятся, только если появились новые ID (и раз в сутки).
- 3 цикла подряд ошибка → `[ALERT]`, прогон красный, письмо от GitHub.

## Как работает фильтр

`pipeline.py`, вызывается из `collect.py` (`Monitor.handle`) со списком, который сбор уже получил:

1. уже увиденные пропускаются (состояние — в Supabase, `seen_listings`);
2. грубый фильтр: тип жилья, Берлин, комнаты, цена (`config.yaml`);
3. для прошедших — подробная страница (Kalt / Nebenkosten / Heizkosten, WBS);
4. расстояние до Boxhagener Platz (координаты с сайта → Nominatim → индекс);
5. оценка для Jobcenter (Bruttokalt);
6. запись в таблицу и в базу — независимо друг от друга.

Если запись в одно из хранилищ не удалась, объявление дописывается туда в
следующих циклах (флаги `written_sheet` / `written_db`). Дублей нет: в таблице
проверяется колонка «ID» (`HOWOGE:1770-24035-55`), для старых строк — ссылка.

## Файлы

| Файл | Что делает |
|---|---|
| `collect.py` | точка входа: сбор в цикле, затем фильтр; `--no-db`, `--no-filter`, `--self-test` |
| `pipeline.py` | фильтры, оценки, запись в таблицу и `listings` |
| `scripts/import_seen_json.py` | разовый перенос `seen.json` старого монитора в `seen_listings` |
| `parsers/` | по модулю на компанию |
| `geo.py` | расстояние до Boxhagener Platz |
| `storage/sheets.py`, `storage/supabase.py` | Google-таблица и Supabase |
| `config.yaml` | фильтры, лимит Jobcenter, слова для отсева по типу жилья |
| `supabase/migrations/` | схема таблиц |
| `tests/` | тесты разбора страниц (`tests/fixtures` — образцы) и логики |
| `kleinanzeigen_collect.py`, `parsers/kleinanzeigen.py` | сбор Kleinanzeigen — только локально (см. ниже) |
| `scripts/*kleinanzeigen*.ps1`, `scripts/copy_chrome_profile.ps1` | Chrome с портом 9222 и задача в Планировщике |
| `.envrc.ps1` | переменные окружения для локального запуска — только `Get-Secret` из Bitwarden, без значений |

## Kleinanzeigen (локально, не в Actions)

Kleinanzeigen банит IP дата-центров, поэтому сбор идёт на домашнем компьютере через
настоящий Chrome с уже выполненным входом. Пишет **только** в Supabase
`collected_listings` (`company = 'Kleinanzeigen'`) — сырые данные без оценки.

Что собирается: ID, ссылка, заголовок, полное описание, Kaltmiete / Warmmiete /
Nebenkosten / Heizkosten / Kaution (что есть), площадь, комнаты, этаж, район и адрес
как на сайте, частный / коммерческий продавец (`seller_type`), имя, телефон и e-mail
(если видны без клика или есть в Impressum), `contact` (userId, «Aktiv seit», сайт
компании, Impressum, есть ли форма отклика и нужен ли вход), дата создания (`published`;
дата «поднятия» — в `raw.detail.bumped_at`), `found_via` — в каких поисках найдено.

Поиски (фильтры — через URL сайта, значения в `config.yaml` → `kleinanzeigen`):
Friedrichshain и Mitte × {все, чекбокс WBS сайта, текстовый поиск «wbs»}, Kaltmiete ≤ 850 €
(черновой ориентир, не лимит Jobcenter), 1–1,5 комнаты. Одно объявление из нескольких
поисков — одна строка. Лента листается до первого уже известного ID. Первый запуск —
«точка отсчёта»: текущие объявления пишутся с `baseline = true` без подробных страниц.

Установка (один раз, PowerShell 7):

```powershell
pip install -r requirements-local.txt
# 1. закрыть Chrome полностью, затем скопировать профиль (порт 9222 для основного профиля Chrome не открывает)
pwsh -File scripts\copy_chrome_profile.ps1
# 2. открыть Chrome с портом 9222 (+ ярлык «Chrome Kleinanzeigen» на рабочем столе); проверить, что вход в Kleinanzeigen есть
pwsh -File scripts\start_kleinanzeigen_chrome.ps1 -CreateShortcut
# 3. секрет Supabase в Bitwarden (имя — как в .envrc.ps1)
# если записи ещё нет: создать в Bitwarden запись «wohnung-monitor-supabase-key» (ключ — в поле пароля), затем разблокировать хранилище
$env:BW_SESSION = bw unlock --raw
# 4. проверка без записи в базу, затем задача каждые 30 минут
python kleinanzeigen_collect.py --no-db --now
pwsh -File scripts\register_kleinanzeigen_task.ps1
```

Поведение: окно Chrome с портом закрыто → запуск ничего не делает и откладывает
следующую попытку на 2 часа (`data/kleinanzeigen/state.json`); капча / блокировка →
стоп целиком, пауза 6 часов, в `parser_runs` — ошибка. Лог — `data/kleinanzeigen/collect.log`.

## Фильтры (config.yaml)

Берлин; до 2 комнат; Warmmiete ≤ 750 € (если неизвестна — Kaltmiete ≤ 600 €);
≤ 3,5 км от Boxhagener Platz, 🟢 до 2 км. Отсеиваются студенческие комнаты,
жильё для пожилых, Gewerbe/Stellplatz/Garage/Lager. WBS — не причина отсева.
Адрес не определился → строка с приоритетом «🟡 расстояние не определено».

Jobcenter: Bruttokalt = Kalt + холодные Nebenkosten, лимит 449 €, с надбавкой ≈ 539 €.
У HOWOGE и WBM отопление входит в Nebenkosten — пишется «Kalt+NK = X (отопление
внутри), точный Bruttokalt уточнить».

## Supabase

| Таблица | Что в ней |
|---|---|
Сбор — `collected_listings`, `listing_changes`, `parser_runs`, `parser_health` (таблица выше). Фильтр:

| Таблица | Что в ней |
|---|---|
| `listings` | прошедшие фильтр объявления (то же, что в Google-таблице) |
| `seen_listings` | все увиденные фильтром, включая отсеянные: причина `reject_reason`, флаги записи |
| `geocache` | кэш геокодера |

## Сигналы о поломке

Если компания 3 цикла подряд вернула ошибку или подозрительное «пусто» — в логе
`[ALERT]`, прогон завершается с ошибкой (красный), GitHub присылает письмо. Это
происходит один раз при переходе в alert. Когда парсер снова работает —
`[RECOVERED]` в логе.

## Запуск вручную

Actions → Сбор объявлений → Run workflow:
- `self_test` — только тестовая строка в таблицу и в `listings` (проверка доступа).

Локально:

```bash
pip install -r requirements.txt
python collect.py --no-db --only WBM   # один цикл, только лог: ни базы, ни таблицы
python -m pytest
```

Переменные: `SUPABASE_KEY` (секретный ключ), `GOOGLE_SERVICE_ACCOUNT_JSON`
или `GOOGLE_SERVICE_ACCOUNT_FILE` (путь к JSON-ключу сервисного аккаунта).
В GitHub они заданы как Secrets.

## Если сайт поменял вёрстку

Тесты в `tests/test_parsers.py` работают на сохранённых страницах. Сохранить
свежую страницу в `tests/fixtures/`, запустить `python -m pytest` — упавший тест
покажет, какое поле перестало разбираться.
