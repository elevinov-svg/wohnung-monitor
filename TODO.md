# TODO

## 1. Общий фильтр / оценщик (этап B) — ещё не написан

Отдельный скрипт, общий для всех источников (landeseigene + Kleinanzeigen):

- читает сырые строки из Supabase `collected_listings` (`baseline = false`, ещё не оценённые —
  состояние в `seen_listings`, как у `pipeline.py`);
- отсеивает WG («1 Zimmer» в WG), Zwischenmiete / Kurzzeitmiete / Tausch — по `title`,
  `description`, `raw.detail.attributes` («Tauschangebot»);
- WBS: `wbs` / `wbs_source` (чекбокс сайта), `raw.detail.wbs_in_text`, `found_via`;
- расстояние до Boxhagener Platz — геокодинг `address` / `postcode` (у Kleinanzeigen координат нет);
- пишет прошедших в Google-таблицу (и `listings`).

Логику фильтров стоит вынести из `pipeline.py`, а не писать заново.

## 2. landeseigene: убрать прямую запись в Google-таблицу (п.7 задачи Kleinanzeigen)

Сейчас `collect.py` в том же цикле зовёт `pipeline.Monitor.handle` — фильтр и запись
в таблицу. Нужно: сбор пишет только в Supabase, таблица получает строки только
от общего фильтра (п.1). **Делать вместе с п.1**, иначе таблица перестанет пополняться.
Отдельным коммитом, чтобы можно было откатить независимо.

## 3. landeseigene: поля для будущего отклика

Проверить, что парсеры компаний заполняют общие поля `description`, `seller_type`
(для компаний — `commercial`), `contact_phone`, `contact_email`, `contact`
(`form: {available, login_required, fields}`, ссылка на форму заявки / Immomio и т.п.).
Сейчас они не заполняются — в `collected_listings` только display-данные.

## 4. Kleinanzeigen: мелочи

- Стоп на первом известном ID обрывает ленту на «поднятых» старых объявлениях
  (Tausch-сервисы поднимаются ежедневно). Если начнут теряться новые — поднять
  `kleinanzeigen.stop_after_known` в `config.yaml` (например, до 5).
- Объявление, уже известное по одной ленте, в другой ленте не дописывает `found_via`
  (лента останавливается на нём).
- Исчезновение объявлений (`disappeared_at`) для Kleinanzeigen не отмечается — полного
  обхода нет.
- Образец страницы капчи (`tests/fixtures/kleinanzeigen_block.html`) — синтетический;
  при первой реальной капче сохранить её и уточнить `BLOCK_MARKERS`.
