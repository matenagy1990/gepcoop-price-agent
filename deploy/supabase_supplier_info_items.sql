create table if not exists public.supplier_info_items (
  id uuid primary key default gen_random_uuid(),
  supplier_id text not null,
  label text not null,
  value text not null,
  sort_order integer not null default 1,
  is_active boolean not null default true,
  updated_at timestamptz not null default now(),
  updated_by text
);

create index if not exists supplier_info_items_supplier_idx
  on public.supplier_info_items (supplier_id, is_active, sort_order);

alter table public.supplier_info_items
  drop constraint if exists supplier_info_items_label_not_blank;

alter table public.supplier_info_items
  add constraint supplier_info_items_label_not_blank
  check (length(btrim(label)) > 0);

alter table public.supplier_info_items
  drop constraint if exists supplier_info_items_value_not_blank;

alter table public.supplier_info_items
  add constraint supplier_info_items_value_not_blank
  check (length(btrim(value)) > 0);

create or replace function public.enforce_supplier_info_items_max_active()
returns trigger
language plpgsql
as $$
begin
  if new.is_active then
    if (
      select count(*)
      from public.supplier_info_items
      where supplier_id = new.supplier_id
        and is_active = true
        and (tg_op = 'INSERT' or id <> new.id)
    ) >= 5 then
      raise exception 'A supplier can have at most 5 active info items.';
    end if;
  end if;
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists supplier_info_items_max_active
  on public.supplier_info_items;

create trigger supplier_info_items_max_active
before insert or update on public.supplier_info_items
for each row execute function public.enforce_supplier_info_items_max_active();

notify pgrst, 'reload schema';
