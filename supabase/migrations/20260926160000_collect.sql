-- Сбор: все объявления шести компаний без фильтров, история изменений,
-- журнал прогонов и состояние парсеров. Обработка (фильтры, Jobcenter,
-- расстояние, таблица) — отдельным этапом, в эту схему не входит.

-- ------------------------------------------------------------------ все объявления
create table public.collected_listings (
  company           text not null,              -- HOWOGE, degewo, WBM, GESOBAU, Gewobag, Stadt und Land
  external_id       text not null,              -- родной ID объекта компании
  link              text not null,
  title             text,
  address           text,
  postcode          text,
  district          text,
  rooms             numeric,
  area              numeric,
  kalt              numeric,
  neben             numeric,                    -- холодные Nebenkosten (или общие, если heiz_in_neben)
  heiz              numeric,
  warm              numeric,
  heiz_in_neben     boolean not null default false,
  deposit           numeric,                    -- Kaution суммой, если указана числом
  deposit_text      text,                       -- Kaution как на сайте
  floor             text,                       -- этаж как на сайте
  available_from    text,                       -- «доступна с» как на сайте
  published         text,                       -- дата публикации с сайта (Gewobag: datePublished)
  lat               double precision,
  lon               double precision,
  wbs               text check (wbs in ('да', 'нет')),   -- null = неизвестно
  wbs_source        text,
  wbs_type          text,
  tags              text,
  prices            jsonb not null default '{}',  -- все ценовые поля как есть: метка -> значение
  raw               jsonb,                        -- {list: …, detail_pairs: …, flags: …}
  detail_ok         boolean,                      -- null = подробная не читалась
  detail_error      text,
  detail_checked_at timestamptz,                  -- последнее чтение подробной страницы
  first_seen        timestamptz not null,
  last_seen         timestamptz not null,
  disappeared_at    timestamptz,                  -- null = сейчас на сайте
  baseline          boolean not null default false,  -- first_seen = первый цикл, а не реальное появление
  primary key (company, external_id)
);
create index collected_listings_active_idx on public.collected_listings (company) where disappeared_at is null;
create index collected_listings_first_seen_idx on public.collected_listings (first_seen);

-- ------------------------------------------------------------------ изменения
-- Одна строка на изменившееся поле. field = 'status' — исчезновение/возвращение
-- (old/new: "active" / "gone").
create table public.listing_changes (
  id          bigint generated always as identity primary key,
  company     text not null,
  external_id text not null,
  changed_at  timestamptz not null,
  field       text not null,
  old_value   jsonb,
  new_value   jsonb,
  source      text,        -- 'список' | 'подробная' | 'наличие'
  foreign key (company, external_id) references public.collected_listings (company, external_id) on delete cascade
);
create index listing_changes_listing_idx on public.listing_changes (company, external_id, changed_at);
create index listing_changes_field_idx on public.listing_changes (field, changed_at);

-- ------------------------------------------------------------------ журнал прогонов
-- Одна строка на компанию за цикл (~каждые 3 минуты).
create table public.parser_runs (
  id             bigint generated always as identity primary key,
  gh_run_id      text,
  company        text not null,
  started_at     timestamptz not null,
  finished_at    timestamptz,
  status         text not null check (status in ('ok', 'empty', 'suspect', 'partial', 'error')),
  error          text,
  items_count    int,       -- разобрано объявлений
  site_count     int,       -- сколько пишет сайт (где есть счётчик)
  new_count      int,
  gone_count     int,
  back_count     int,
  changed_count  int,       -- объявлений с изменениями полей
  detail_new     int,       -- прочитано подробных: новые
  detail_refresh int,       -- прочитано подробных: суточная перепроверка
  detail_errors  int,
  requests       int,
  http_errors    int,
  retries        int,       -- сработавших повторов (таймаут / соединение / 502-504)
  list_sec       numeric,
  detail_sec     numeric,
  meta           jsonb      -- служебное от парсера: страницы, фильтры WBS, предупреждения
);
create index parser_runs_company_idx on public.parser_runs (company, started_at desc);

-- ------------------------------------------------------------------ состояние парсеров
create table public.parser_health (
  company      text primary key,
  state        text not null default 'ok' check (state in ('ok', 'suspect', 'alert')),
  fail_streak  int not null default 0,     -- циклов подряд с ошибкой / подозрительным «пусто»
  empty_streak int not null default 0,     -- циклов подряд «0 объявлений» при валидной структуре
  last_count   int,                        -- последнее ненулевое число объявлений
  last_ok_at   timestamptz,
  since        timestamptz not null default now(),   -- с какого момента текущее state
  last_error   text,
  meta         jsonb not null default '{}',          -- напр. {"wbs_filters_at": "…"} для Gewobag
  updated_at   timestamptz not null default now()
);

-- ------------------------------------------------------------------ отметка «видели в этом цикле»
-- last_seen = p_ts для всех p_ids; вернувшиеся — disappeared_at = null;
-- при p_mark_gone — пропавшие из списка получают disappeared_at = p_ts.
-- События исчезновения/возвращения пишутся в listing_changes (field = 'status').
create function public.collect_mark_seen(p_company text, p_ids text[], p_ts timestamptz, p_mark_gone boolean)
returns table (external_id text, event text)
language plpgsql
set search_path = ''
as $$
begin
  return query
  with back as (
    update public.collected_listings l
       set disappeared_at = null, last_seen = p_ts
     where l.company = p_company and l.external_id = any (p_ids) and l.disappeared_at is not null
    returning l.external_id
  ), seen as (
    update public.collected_listings l
       set last_seen = p_ts
     where l.company = p_company and l.external_id = any (p_ids) and l.disappeared_at is null
    returning l.external_id
  ), gone as (
    update public.collected_listings l
       set disappeared_at = p_ts
     where p_mark_gone and l.company = p_company and l.disappeared_at is null
       and not (l.external_id = any (p_ids))
    returning l.external_id
  ), ev as (
    select b.external_id, 'back'::text as event from back b
    union all
    select g.external_id, 'gone'::text from gone g
  ), logged as (
    insert into public.listing_changes (company, external_id, changed_at, field, old_value, new_value, source)
    select p_company, ev.external_id, p_ts, 'status',
           to_jsonb(case ev.event when 'back' then 'gone' else 'active' end),
           to_jsonb(case ev.event when 'back' then 'active' else 'gone' end),
           'наличие'
      from ev
  )
  select ev.external_id, ev.event from ev;
end;
$$;

revoke execute on function public.collect_mark_seen(text, text[], timestamptz, boolean) from public, anon, authenticated;

-- доступ только секретным ключом (service_role обходит RLS), политик нет
alter table public.collected_listings enable row level security;
alter table public.listing_changes    enable row level security;
alter table public.parser_runs        enable row level security;
alter table public.parser_health      enable row level security;
