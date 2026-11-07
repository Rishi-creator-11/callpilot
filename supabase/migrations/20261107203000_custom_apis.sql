-- Owner-defined HTTPS APIs the receptionist may call during a call. Secrets are encrypted by the app.
create table public.custom_apis (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete cascade,
    tool_name text not null check (tool_name ~ '^api_[a-z0-9_]{2,40}$'),
    name text not null check (char_length(name) between 2 and 60),
    description text not null check (char_length(description) between 10 and 400),
    method text not null check (method in ('GET', 'POST')),
    url_template text not null check (char_length(url_template) <= 500),
    params jsonb not null default '[]'::jsonb,
    auth_ciphertext text,
    enabled boolean not null default true,
    created_by uuid,
    created_at timestamptz not null default now(),
    unique (organization_id, tool_name)
);

create table public.custom_api_audit (
    id bigint generated always as identity primary key,
    organization_id uuid not null references public.organizations(id) on delete cascade,
    custom_api_id uuid references public.custom_apis(id) on delete set null,
    provider_call_id text,
    outcome text not null,
    status_code integer,
    created_at timestamptz not null default now()
);
create index custom_api_audit_workspace_idx on public.custom_api_audit (organization_id, created_at desc);

alter table public.custom_apis enable row level security;
alter table public.custom_api_audit enable row level security;
revoke all on public.custom_apis from public, anon, authenticated;
revoke all on public.custom_api_audit from public, anon, authenticated;
grant select, insert, update, delete on public.custom_apis to service_role;
grant select, insert on public.custom_api_audit to service_role;
