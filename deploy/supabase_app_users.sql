create table if not exists public.app_users (
  username text primary key,
  password_hash text not null,
  is_admin boolean not null default false,
  is_primary boolean not null default false,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  deleted_at timestamptz
);

create unique index if not exists app_users_primary_unique_idx
  on public.app_users ((is_primary))
  where is_primary = true;

create index if not exists app_users_active_idx
  on public.app_users (is_active, username);

alter table public.app_users
  add constraint app_users_primary_admin_chk
  check (not is_primary or is_admin);
