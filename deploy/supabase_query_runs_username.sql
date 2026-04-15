alter table public.query_runs
  add column if not exists username text;

create index if not exists query_runs_username_idx
  on public.query_runs (username, started_at desc);
