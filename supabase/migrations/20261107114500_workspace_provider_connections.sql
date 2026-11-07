create table public.workspace_provider_connections (
    organization_id uuid not null references public.organizations(id) on delete cascade,
    provider text not null check (provider in ('highlevel')),
    account_ref text not null,
    account_name text not null,
    token_ciphertext text not null,
    connected_at timestamptz not null default now(),
    primary key (organization_id, provider)
);

alter table public.workspace_provider_connections enable row level security;
revoke all on public.workspace_provider_connections from public, anon, authenticated;
grant select, insert, update, delete on public.workspace_provider_connections to service_role;
