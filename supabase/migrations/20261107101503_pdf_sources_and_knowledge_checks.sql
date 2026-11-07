alter table public.knowledge_documents
    add column source_filename text check (char_length(source_filename) <= 200),
    add column source_sha256 text check (source_sha256 ~ '^[0-9a-f]{64}$');

create unique index knowledge_documents_organization_sha_idx
    on public.knowledge_documents (organization_id, source_sha256);

create table public.knowledge_checks (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete cascade,
    question text not null check (char_length(question) between 3 and 500),
    expectation text not null check (expectation in ('contains', 'unknown', 'policy')),
    expected_text text not null default '' check (char_length(expected_text) <= 300),
    created_at timestamptz not null default now(),
    check ((expectation = 'contains') = (length(btrim(expected_text)) > 0))
);

create index knowledge_checks_workspace_order_idx
    on public.knowledge_checks (organization_id, created_at, id);

alter table public.knowledge_checks enable row level security;
revoke all on public.knowledge_checks from public, anon, authenticated;
grant select, insert, delete on public.knowledge_checks to service_role;
