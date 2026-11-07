create table public.shopify_shipment_attempts (
    organization_id uuid not null references public.organizations(id) on delete cascade,
    order_request_id uuid not null references public.order_requests(id) on delete cascade,
    fulfillment_order_id text not null,
    attempt_id uuid not null,
    tracking_number text not null,
    carrier text not null,
    status text not null check (status in ('pending','failed','recorded')),
    fulfillment_id text,
    primary key (organization_id,order_request_id,fulfillment_order_id),
    check ((status='recorded') = (fulfillment_id is not null))
);
alter table public.shopify_shipment_attempts enable row level security;
revoke all on public.shopify_shipment_attempts from public,anon,authenticated;
grant select,insert,update on public.shopify_shipment_attempts to service_role;
