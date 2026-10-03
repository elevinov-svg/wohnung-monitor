# Wohnung-Monitor

Мониторинг квартир в Берлине: парсит источники объявлений, пишет **сырые данные** в Supabase,
фильтрует и записывает **подходящие квартиры** в Google Sheet.

## Архитектура (новая)

Два независимых скрипта:

1. **`monitor.py`** (GitHub Actions, каждые 30 мин) — парсит источники, пишет **все объявления** (сырые данные) в Supabase
2. **`filter.py`** (сервер, каждые 30 мин) — читает Supabase, фильтрует (WG/Zwischenmiete/расстояние), пишет **подходящие** в Google Sheet

### Источники

- **Landeseigene** (HOWOGE, degewo, WBM, GESOBAU, Gewobag, Stadt und Land) — работает в Actions
- **Kleinanzeigen** — локально на ноутбуке через Chrome remote-debugging (чтобы не ловить бан)
- **FriedrichsHeim eG** RSS-фиды

Telegram-уведомления пока выключены (`USE_TELEGRAM=false` по умолчанию) —
код рабочий, включается одной переменной окружения, когда понадобится.

## Как запустить (15 минут, один раз)

### 1. Google Sheets — настройка сервисного аккаунта

Это отдельная техническая учётная запись Google (не твой личный логин),
у которой будет доступ только к этой одной таблице — мы дадим его сами.

1. Зайти в [Google Cloud Console](https://console.cloud.google.com/),
   создать новый проект (или использовать существующий).
2. **APIs & Services → Library** → найти "Google Sheets API" → **Enable**.
3. **APIs & Services → Credentials → Create Credentials → Service Account**.
   Дать любое имя, роль можно не назначать (не нужна — доступ дадим
   напрямую в таблице).
4. Открыть созданный сервисный аккаунт → вкладка **Keys** →
   **Add Key → Create new key → JSON**. Скачается файл `xxxx.json` —
   это и есть содержимое для `GOOGLE_SERVICE_ACCOUNT_JSON`.
5. В скачанном JSON найти поле `"client_email"` — это будет что-то вроде
   `wohnung-bot@твой-проект.iam.gserviceaccount.com`.
6. Открыть саму [таблицу](https://docs.google.com/spreadsheets/d/1rpcX6ViQtSUcRiGYvAKK-Kb703rwCsrjNmr_CmgJNKs/edit),
   нажать **"Настройки доступа"** ("Share"), добавить туда этот email
   с правом **"Редактор"**.

### 2. Репозиторий на GitHub

Готово — репозиторий `wohnung-monitor` создан, все файлы загружены
через GitHub API.

### 3. Добавить секрет в GitHub

В репозитории: **Settings → Secrets and variables → Actions → New repository secret**

- `GOOGLE_SERVICE_ACCOUNT_JSON` — вставить **всё содержимое** скачанного
  JSON-файла целиком (открыть его текстовым редактором, скопировать всё).

Telegram-секреты (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) пока можно не
создавать — они не используются, пока `USE_TELEGRAM` не включён.

### 4. Проверить

Вкладка **Actions** в репозитории → workflow "Wohnung Monitor" →
**Run workflow** (запустить вручную, не ждать 30 минут). Через минуту-две
в логе будет видно, что скрипт отработал. Если новых объявлений нет —
ничего не запишется в таблицу, это нормально. Если что-то пошло не так
с доступом к таблице — в логе будет понятная ошибка (например,
"permission denied", если забыл дать сервисному аккаунту доступ на шаге 1.6).

Дальше он будет сам запускаться каждые 30 минут по расписанию в файле
`.github/workflows/monitor.yml`.

## Supabase — база данных

Проект Supabase: `wohnung-monitor` (https://vbkrmzjmmbhtnqjotrnz.supabase.co),
таблица `public.listings`. Туда пишут **все** скрипты: облачный `monitor.py`,
локальный `local/local_monitor.py` и Kleinanzeigen-скрипт.

- **Сырые данные** (все объявления) — в Supabase
- **Отфильтрованные** (подходящие) — в Google Sheet

Код записи — `db.py` (без новых зависимостей, через REST API).

**Настройка (один раз):**
1. Supabase → проект `wohnung-monitor` → **Project Settings → API Keys** →
   скопировать **секретный** ключ (`service_role` key). Никому не показывать и не класть в репозиторий.
2. GitHub → репозиторий → **Settings → Secrets and variables → Actions →
   New repository secret**: имя `SUPABASE_KEY`, значение — этот ключ.
3. На сервере: добавить в Bitwarden (`add_secret "wohnung-monitor-supabase-key" "<key>"`)
4. Проверка: Actions → Wohnung Monitor → Run workflow с галкой `self_test` —
   в базу запишется тестовая строка.

## Настройка фильтра на сервере

`filter.py` запускается на сервере каждые 30 минут через systemd timer.

**1. Создать .env файл с секретами:**
```bash
source ~/.bitwarden-functions.sh
export BW_SESSION=$(bw unlock --raw)
generate_env_file ~/.secrets/wohnung-filter.env \
    "wohnung-monitor-supabase-key:SUPABASE_KEY" \
    "GOOGLE_SERVICE_ACCOUNT_JSON:GOOGLE_SERVICE_ACCOUNT_JSON"
```

**2. Установить Python-зависимости:**
```bash
cd ~/dev/wohnung-monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**3. Установить systemd service и timer:**
```bash
sudo cp wohnung-filter.service /etc/systemd/system/
sudo cp wohnung-filter.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable wohnung-filter.timer
sudo systemctl start wohnung-filter.timer
```

**4. Проверить:**
```bash
# Запустить вручную
sudo systemctl start wohnung-filter.service

# Посмотреть логи
journalctl -u wohnung-filter.service -n 50

# Статус timer
systemctl status wohnung-filter.timer
```

## Как добавить следующий источник

1. Открой сайт в браузере, найди страницу со списком объявлений.
2. Если есть RSS (иконка/ссылка "RSS", "Feed", "Abonnieren") — это самый
   надёжный вариант, добавь блок `type: rss` в `sources.yaml`.
3. Если RSS нет — `type: html_links`: нужен URL страницы со списком и
   `link_pattern` — кусок URL, характерный именно для ссылок на
   объявления (посмотреть в исходном коде страницы, Ctrl+U).
4. Если объявления подгружаются через JavaScript (то есть в исходном
   коде страницы их не видно) — html_links не сработает, скажи мне,
   сделаем через браузерную автоматизацию отдельно.

## Ограничения этой версии (осознанно, чтобы не усложнять сразу)

- Проверяет по расписанию (раз в 30 мин), а не мгновенно.
- Не подаёт заявки автоматически — только уведомляет. Это следующий шаг.
- html_links не работает с сайтами, где список рендерится через JS.
- Состояние («что уже видели») пока по-прежнему в JSON-файлах репозитория;
  база Supabase — хранилище самих объявлений.
