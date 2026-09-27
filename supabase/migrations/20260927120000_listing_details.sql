-- Общие поля объявления для всех источников (landeseigene + Kleinanzeigen):
-- полное описание, тип продавца, контакты и через какие поиски найдено.
-- Отдельной таблицы под Kleinanzeigen нет: company = 'Kleinanzeigen' в collected_listings.
-- Поля — с заделом на полуавтоматический отклик (есть ли форма на сайте, нужен ли логин).

alter table public.collected_listings
  add column if not exists description   text,                     -- полный текст объявления, без обрезки
  add column if not exists seller_type   text check (seller_type in ('private', 'commercial')),  -- null = неизвестно
  add column if not exists seller_name   text,                     -- имя / название продавца как на сайте
  add column if not exists contact_phone text,                     -- телефон, если виден без клика / в Impressum
  add column if not exists contact_email text,                     -- e-mail, если указан (Impressum)
  -- всё остальное про контакт: {user_id, user_since, profile_url, website, imprint,
  --   form: {available, login_required, fields}, phone_login_required}
  add column if not exists contact       jsonb not null default '{}',
  -- через какие поиски найдено: Kleinanzeigen — 'general' / 'wbs_filter' / 'wbs_text' (с районом)
  add column if not exists found_via     text[] not null default '{}';

create index if not exists collected_listings_found_via_idx on public.collected_listings using gin (found_via);
