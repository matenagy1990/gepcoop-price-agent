alter table public.query_runs
  add column if not exists recommendation_feedback text,
  add column if not exists recommendation_feedback_at timestamptz,
  add column if not exists recommendation_feedback_by text;

alter table public.query_runs
  drop constraint if exists query_runs_recommendation_feedback_chk;

alter table public.query_runs
  add constraint query_runs_recommendation_feedback_chk
  check (
    recommendation_feedback is null
    or recommendation_feedback in ('positive', 'negative')
  );

create index if not exists query_runs_recommendation_feedback_idx
  on public.query_runs (recommendation_feedback, recommendation_feedback_at desc);
