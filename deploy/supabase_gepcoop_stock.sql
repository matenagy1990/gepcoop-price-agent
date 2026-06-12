-- Gepcoop sajat keszlet: fuggetlen tabla a webshop-mappingtol.
-- Futtasd le egyszer a Supabase SQL editorban.
-- Az admin "Gepcoop keszlet" feltoltes minden alkalommal teljesen lecsereli a tartalmat.
-- Oszlopok: part_no=Cikkszam, name=Cikknev, sellable=Elado,
--           last_purchase_price=Telephelyi utolso besz ar,
--           last_purchase_date=Telephelyi utolso besz datum, selling_price=Eladasi ar.

create table if not exists gepcoop_stock (
    part_no text primary key,
    name text,
    sellable text,
    last_purchase_price text,
    last_purchase_date text,
    selling_price text,
    updated_at timestamptz default now()
);
