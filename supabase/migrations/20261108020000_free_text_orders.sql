-- Orders for businesses whose products are not catalog SKUs (a print shop quotes by size, stock and finish).
-- The receptionist saves what the caller asked for; staff review it. Nothing here changes any catalog or price.
alter table public.order_requests
    alter column item_id drop not null,
    alter column sku drop not null,
    add column if not exists product_text text,
    add column if not exists details text;
alter table public.order_requests
    add constraint order_requests_has_item check (sku is not null or product_text is not null);
