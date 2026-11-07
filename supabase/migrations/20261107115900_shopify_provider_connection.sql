alter table public.workspace_provider_connections
    drop constraint workspace_provider_connections_provider_check,
    add constraint workspace_provider_connections_provider_check
    check (provider in ('highlevel', 'shopify'));
