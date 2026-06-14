-- Batch Price Agent – Supabase táblák
-- Futtasd a Supabase SQL Editor-ban

-- ──────────────────────────────────────────────────────────────────────────────
-- batch_runs: egy projekt/futás fő adatai
-- ──────────────────────────────────────────────────────────────────────────────
create table if not exists batch_runs (
    id                      uuid primary key default gen_random_uuid(),
    project_name            text not null,
    created_at              timestamptz not null default now(),
    status                  text not null default 'running',   -- running | completed | partial | failed
    selected_suppliers      text[] not null default '{}',
    total_input_count       int  not null default 0,
    unique_part_count       int  not null default 0,
    searchable_count        int  not null default 0,
    missing_mapping_count   int  not null default 0,
    success_count           int  not null default 0,
    error_count             int  not null default 0,
    completed_at            timestamptz,
    duration_ms             int
);

create index if not exists batch_runs_created_at_idx on batch_runs (created_at desc);

-- ──────────────────────────────────────────────────────────────────────────────
-- batch_run_items: bemeneti Gép-Coop cikkszámok
-- ──────────────────────────────────────────────────────────────────────────────
create table if not exists batch_run_items (
    id              uuid primary key default gen_random_uuid(),
    batch_run_id    uuid not null references batch_runs(id) on delete cascade,
    row_index       int  not null,
    gepcoop_part_no text not null,
    product_name    text,
    status          text not null default 'pending',  -- pending | searchable | no_mapping | not_in_db
    duplicate_of    text,
    created_at      timestamptz not null default now()
);

create index if not exists batch_run_items_run_idx on batch_run_items (batch_run_id);

-- ──────────────────────────────────────────────────────────────────────────────
-- batch_run_supplier_results: cikkszám × supplier eredmények
-- ──────────────────────────────────────────────────────────────────────────────
create table if not exists batch_run_supplier_results (
    id                  uuid primary key default gen_random_uuid(),
    batch_run_id        uuid not null references batch_runs(id) on delete cascade,
    batch_run_item_id   uuid references batch_run_items(id) on delete set null,
    gepcoop_part_no     text not null,
    supplier_id         text not null,
    supplier_part_no    text,
    mapping_status      text not null default 'mapped',  -- mapped | missing_mapping | not_in_db
    scrape_status       text not null default 'pending', -- ok | not_found | not_priced | timeout | login_failed | error | skipped
    raw_price           numeric(14,4),
    currency            text,
    unit                text,
    normalized_price_huf numeric(14,4),
    stock_raw           text,
    stock_quantity      int,
    is_top              boolean not null default false,
    is_alternative      boolean not null default false,
    error_message       text,
    started_at          timestamptz,
    completed_at        timestamptz,
    duration_ms         int
);

create index if not exists batch_results_run_idx  on batch_run_supplier_results (batch_run_id);
create index if not exists batch_results_part_idx on batch_run_supplier_results (gepcoop_part_no);

-- ──────────────────────────────────────────────────────────────────────────────
-- batch_run_events: opcionális audit log
-- ──────────────────────────────────────────────────────────────────────────────
create table if not exists batch_run_events (
    id              uuid primary key default gen_random_uuid(),
    batch_run_id    uuid not null references batch_runs(id) on delete cascade,
    event_type      text not null,
    supplier_id     text,
    gepcoop_part_no text,
    message         text,
    created_at      timestamptz not null default now()
);

create index if not exists batch_events_run_idx on batch_run_events (batch_run_id);
