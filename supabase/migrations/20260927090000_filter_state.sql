-- Фильтр и Google-таблица внутри collect.py: состояние фильтра и кэш геокодера.
-- parser_runs / parser_health — из 20260926160000_collect.sql (их ведёт сбор).

-- ВСЕ увиденные фильтром объявления: решение фильтра и куда уже записано
create table if not exists public.seen_listings (
  company       text not null,
  external_id   text not null,
  link          text,
  first_seen    timestamptz not null default now(),
  last_seen     timestamptz not null default now(),
  current_since timestamptz not null default now(),   -- начало текущей публикации (для повторных)
  passed_filter boolean,                               -- null = решение не принято, повторить
  reject_reason text,
  written_sheet boolean not null default false,
  written_db    boolean not null default false,
  legacy        boolean not null default false,        -- перенесено из seen.json старого монитора
  raw           jsonb,
  primary key (company, external_id)
);
create index if not exists seen_listings_link_idx on public.seen_listings (link);
create index if not exists seen_listings_last_seen_idx on public.seen_listings (company, last_seen desc);

-- кэш геокодера (Nominatim): запрос -> координаты, null = точно не найдено
create table if not exists public.geocache (
  query      text primary key,
  lat        double precision,
  lon        double precision,
  updated_at timestamptz not null default now()
);

-- pipeline.db_record пишет координаты
alter table public.listings add column if not exists lat double precision;
alter table public.listings add column if not exists lon double precision;

-- единственная старая строка: ключ по ссылке -> родной ID объекта
update public.listings
   set external_id = '0100-02509-0111-0201', listing_key = 'Gewobag:0100-02509-0111-0201'
 where source = 'Gewobag'
   and external_id = 'https://www.gewobag.de/fuer-mietinteressentinnen/mietangebote/0100-02509-0111-0201/';

-- доступ только секретным ключом (service_role обходит RLS), политик нет
alter table public.seen_listings enable row level security;
alter table public.geocache      enable row level security;
