alter table public.catalog_items
    add column use_shopify_inventory boolean not null default false;
