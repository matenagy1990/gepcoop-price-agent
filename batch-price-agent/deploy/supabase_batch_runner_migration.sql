-- Batch Price Agent - futtato neve es szuresi indexek
-- Futtasd egyszer a Supabase SQL Editorban.

alter table public.batch_runs
    add column if not exists runner_name text;

create extension if not exists pg_trgm;

create index if not exists batch_runs_project_name_trgm_idx
    on public.batch_runs using gin (project_name gin_trgm_ops);

create index if not exists batch_runs_runner_name_trgm_idx
    on public.batch_runs using gin (runner_name gin_trgm_ops);

comment on column public.batch_runs.runner_name is
    'A Batch Price Agent futast indito bejelentkezett felhasznaloneve.';
