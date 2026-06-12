-- Argip price list table -- full 1:1 mapping of the Argip Excel columns.
-- Run in the Supabase SQL Editor. Drop & recreate is safe since the
-- admin upload always replaces all data anyway.

drop table if exists argip_price_list;

create table argip_price_list (
    ean_code              text primary key,   -- ARGIP EAN code (column C)
    ean_code_line         text,               -- EAN code line
    list_name             text,               -- List name
    argip_part_no         text,               -- Argip part #
    description_pl        text,               -- Argip description PL
    description_en        text,               -- Argip description EN
    customer_description  text,               -- Customer description
    customer_part_no      text,               -- Customer part #
    hs_code               text,               -- HS code
    country_of_origin     text,               -- Country of Origin
    discount_pct          numeric,            -- Discount(%) with B2B disc. Incl.
    base_price_eur        numeric,            -- Base price EUR
    price_lvl_1_eur       numeric,            -- Price LVL 1 EUR
    moq_lvl_1_pcs         integer,            -- MOQ LVL1 (pcs)
    price_lvl_2_eur       numeric,            -- Price LVL 2 EUR
    moq_lvl_2_pcs         integer,            -- MOQ LVL 2 (pcs)
    box_quantity_pcs      integer,            -- Box quantity (pcs)
    box_weight_kg         numeric,            -- Box weight kg
    asortyment_id         text,               -- AsortymentID
    asortyment_bazowy_id  text,               -- AsortymentBazowyID
    stan_kart_spec_pcs    text,               -- Stan kart. spec (szt)
    stock_pcs             integer,            -- Stock (pcs)
    dose                  numeric,            -- Dose
    sku                   text,               -- SKU
    updated_at            timestamptz default now()
);
