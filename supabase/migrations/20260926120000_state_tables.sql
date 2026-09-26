-- Состояние мониторинга в Supabase вместо seen.json / geocache.json.
-- НЕ ПРИМЕНЕНО: ждёт ОК.

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
  legacy        boolean not null default false,
  raw           jsonb,
  primary key (company, external_id)
);
create index if not exists seen_listings_link_idx on public.seen_listings (link);
create index if not exists seen_listings_last_seen_idx on public.seen_listings (company, last_seen desc);

create table if not exists public.parser_runs (
  id            bigint generated always as identity primary key,
  company       text not null,
  started_at    timestamptz not null,
  finished_at   timestamptz,
  items_count   int,
  new_count     int,
  passed_count  int,
  sheet_written int,
  db_written    int,
  write_errors  int,
  error         text,
  gh_run_id     text
);
create index if not exists parser_runs_company_idx on public.parser_runs (company, started_at desc);

create table if not exists public.parser_health (
  company     text primary key,
  state       text not null default 'ok' check (state in ('ok', 'alert')),
  fail_streak int not null default 0,
  since       timestamptz not null default now(),
  last_error  text,
  updated_at  timestamptz not null default now()
);

create table if not exists public.geocache (
  query      text primary key,
  lat        double precision,
  lon        double precision,
  updated_at timestamptz not null default now()
);

alter table public.listings add column if not exists lat double precision;
alter table public.listings add column if not exists lon double precision;

-- единственная старая строка: ключ по ссылке -> родной ID объекта
update public.listings
   set external_id = '0100-02509-0111-0201', listing_key = 'Gewobag:0100-02509-0111-0201'
 where source = 'Gewobag'
   and external_id = 'https://www.gewobag.de/fuer-mietinteressentinnen/mietangebote/0100-02509-0111-0201/';

alter table public.seen_listings enable row level security;
alter table public.parser_runs   enable row level security;
alter table public.parser_health enable row level security;
alter table public.geocache      enable row level security;
