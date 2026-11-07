create table public.runtime_worker_forms (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete cascade,
    worker_name text not null,
    revision uuid not null default gen_random_uuid(),
    configuration jsonb not null check (jsonb_typeof(configuration)='object'),
    unique (organization_id,worker_name)
);
create table public.runtime_worker_submissions (
    organization_id uuid not null references public.organizations(id) on delete cascade,
    form_id uuid not null references public.runtime_worker_forms(id) on delete cascade,
    provider_call_id text not null,
    worker_name text not null,
    answers jsonb not null check (jsonb_typeof(answers)='array'),
    created_at timestamptz not null default now(),
    primary key (organization_id,form_id,provider_call_id)
);
create index runtime_worker_submissions_recent
    on public.runtime_worker_submissions(organization_id,form_id,created_at desc);
alter table public.runtime_worker_forms enable row level security;
alter table public.runtime_worker_submissions enable row level security;
revoke all on public.runtime_worker_forms,public.runtime_worker_submissions from public,anon,authenticated;
grant select,insert,update on public.runtime_worker_forms to service_role;
grant select,insert on public.runtime_worker_submissions to service_role;
