-- One Google Sheets connection per workspace, using the non-sensitive drive.file scope.
-- CallPilot creates the spreadsheet itself, so it can only touch files it created.
create table public.sheet_connections (
    organization_id uuid primary key references public.organizations(id) on delete cascade,
    refresh_token_ciphertext text not null,
    spreadsheet_id text not null,
    spreadsheet_url text not null,
    connected_by uuid,
    connected_at timestamptz not null default now()
);

-- One exported row per call, so retries and repeated tool calls never duplicate a lead.
create table public.sheet_exports (
    organization_id uuid not null references public.organizations(id) on delete cascade,
    provider_call_id text not null,
    exported_at timestamptz not null default now(),
    primary key (organization_id, provider_call_id)
);

alter table public.sheet_connections enable row level security;
alter table public.sheet_exports enable row level security;
revoke all on public.sheet_connections from public, anon, authenticated;
revoke all on public.sheet_exports from public, anon, authenticated;
grant select, insert, update, delete on public.sheet_connections to service_role;
grant select, insert, delete on public.sheet_exports to service_role;
