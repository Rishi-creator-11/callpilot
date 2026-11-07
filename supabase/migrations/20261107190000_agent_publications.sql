-- Immutable snapshots of each explicit publish or rollback of the managed receptionist.
create table public.agent_publications (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete cascade,
    version integer not null check (version > 0),
    agent_name text not null,
    greeting text not null,
    call_instructions text not null,
    settings jsonb not null default '{}'::jsonb,
    source text not null default 'publish' check (source in ('publish', 'rollback')),
    rolled_back_from uuid references public.agent_publications(id) on delete set null,
    note text check (note is null or char_length(note) <= 200),
    published_by uuid,
    created_at timestamptz not null default now(),
    unique (organization_id, version)
);

create index agent_publications_workspace_version_idx
    on public.agent_publications (organization_id, version desc);

alter table public.agent_publications enable row level security;
revoke all on public.agent_publications from public, anon, authenticated;
grant select, insert on public.agent_publications to service_role;

-- Records a publication only if the saved configuration still equals what was synced.
-- The agent_configs row lock serializes version numbering per workspace.
create function public.record_agent_publication(
    p_organization_id uuid,
    p_agent_name text,
    p_greeting text,
    p_call_instructions text,
    p_settings jsonb,
    p_user uuid,
    p_note text,
    p_source text,
    p_rolled_back_from uuid
) returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
    current_config public.agent_configs%rowtype;
    next_version integer;
begin
    select * into current_config
    from public.agent_configs
    where organization_id = p_organization_id
    for update;
    if not found then
        return null;
    end if;
    if (current_config.agent_name, current_config.greeting, current_config.call_instructions)
        is distinct from (p_agent_name, p_greeting, p_call_instructions) then
        return null;
    end if;
    select coalesce(max(version), 0) + 1 into next_version
    from public.agent_publications
    where organization_id = p_organization_id;
    insert into public.agent_publications (
        organization_id, version, agent_name, greeting, call_instructions,
        settings, source, rolled_back_from, note, published_by
    ) values (
        p_organization_id, next_version, p_agent_name, p_greeting, p_call_instructions,
        coalesce(p_settings, '{}'::jsonb), p_source, p_rolled_back_from, p_note, p_user
    );
    return next_version;
end;
$$;

revoke all on function public.record_agent_publication(uuid, text, text, text, jsonb, uuid, text, text, uuid)
    from public, anon, authenticated;
grant execute on function public.record_agent_publication(uuid, text, text, text, jsonb, uuid, text, text, uuid)
    to service_role;
