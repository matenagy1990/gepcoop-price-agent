create table if not exists public.copilot_tasks (
  id text primary key,
  customer_id text,
  user_id text,
  title text not null,
  problem_type text not null,
  webshop text not null,
  product_number text not null,
  description text not null,
  expected_result text,
  summary text,
  status text not null default 'open',
  admin_note text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.copilot_tasks
  drop constraint if exists copilot_tasks_status_chk;

alter table public.copilot_tasks
  add constraint copilot_tasks_status_chk
  check (status in ('open', 'in_progress', 'resolved'));

alter table public.copilot_tasks
  drop constraint if exists copilot_tasks_problem_type_chk;

alter table public.copilot_tasks
  add constraint copilot_tasks_problem_type_chk
  check (problem_type in (
    'missing_price',
    'wrong_price',
    'missing_stock',
    'error_message',
    'slow_search',
    'other'
  ));

create index if not exists copilot_tasks_created_at_idx
  on public.copilot_tasks (created_at desc);

create index if not exists copilot_tasks_status_idx
  on public.copilot_tasks (status, created_at desc);

create table if not exists public.copilot_conversations (
  id text primary key,
  task_id text references public.copilot_tasks(id) on delete cascade,
  customer_id text,
  user_id text,
  created_at timestamptz not null default now()
);

create index if not exists copilot_conversations_task_idx
  on public.copilot_conversations (task_id);

create table if not exists public.copilot_messages (
  id text primary key,
  conversation_id text not null references public.copilot_conversations(id) on delete cascade,
  sender text not null,
  message text not null,
  created_at timestamptz not null default now()
);

alter table public.copilot_messages
  drop constraint if exists copilot_messages_sender_chk;

alter table public.copilot_messages
  add constraint copilot_messages_sender_chk
  check (sender in ('user', 'assistant'));

create index if not exists copilot_messages_conversation_idx
  on public.copilot_messages (conversation_id, created_at);

