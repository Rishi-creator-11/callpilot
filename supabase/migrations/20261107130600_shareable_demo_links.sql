create table public.workspace_demo_links (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete cascade,
    label text not null check (char_length(label) between 2 and 70),
    created_at timestamptz not null default now(),
    expires_at timestamptz not null default (now() + interval '7 days'),
    revoked_at timestamptz,
    question_count integer not null default 0,
    minute_started_at timestamptz,
    minute_questions integer not null default 0,
    day_started_at timestamptz,
    day_questions integer not null default 0
);
create index workspace_demo_links_workspace_idx on public.workspace_demo_links (organization_id, created_at desc);
alter table public.workspace_demo_links enable row level security;
revoke all on public.workspace_demo_links from public, anon, authenticated;
grant select, insert, update on public.workspace_demo_links to service_role;

create function public.create_workspace_demo(p_workspace uuid, p_label text)
returns uuid language plpgsql security invoker set search_path = '' as $$
declare result uuid;
begin
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(p_workspace::text, 125982));
    if (select count(*) from public.workspace_demo_links
        where organization_id = p_workspace and revoked_at is null and expires_at > now()) >= 5 then
        return null;
    end if;
    insert into public.workspace_demo_links (organization_id, label)
    values (p_workspace, p_label) returning id into result;
    return result;
end;
$$;

create function public.reserve_workspace_demo_question(p_id uuid)
returns uuid language plpgsql security invoker set search_path = '' as $$
declare
    workspace uuid;
    minute_boundary timestamptz := date_trunc('minute', now());
    day_boundary timestamptz := date_trunc('day', now(), 'UTC');
begin
    update public.workspace_demo_links
    set question_count = question_count + 1,
        minute_questions = case when minute_started_at = minute_boundary then minute_questions + 1 else 1 end,
        minute_started_at = minute_boundary,
        day_questions = case when day_started_at = day_boundary then day_questions + 1 else 1 end,
        day_started_at = day_boundary
    where id = p_id and revoked_at is null and expires_at > now()
      and (minute_started_at is distinct from minute_boundary or minute_questions < 5)
      and (day_started_at is distinct from day_boundary or day_questions < 40)
    returning organization_id into workspace;
    return workspace;
end;
$$;
revoke all on function public.create_workspace_demo(uuid,text) from public, anon, authenticated;
revoke all on function public.reserve_workspace_demo_question(uuid) from public, anon, authenticated;
grant execute on function public.create_workspace_demo(uuid,text) to service_role;
grant execute on function public.reserve_workspace_demo_question(uuid) to service_role;
