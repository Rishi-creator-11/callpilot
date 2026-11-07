create table public.agent_configuration_history (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete cascade,
    agent_name text not null,
    greeting text not null,
    call_instructions text not null,
    created_at timestamptz not null default now()
);

create index agent_configuration_history_workspace_created_idx
    on public.agent_configuration_history (organization_id, created_at desc);

alter table public.agent_configuration_history enable row level security;
revoke all on public.agent_configuration_history from public, anon, authenticated;
grant select, insert on public.agent_configuration_history to service_role;

create function public.save_agent_configuration(
    p_organization_id uuid,
    p_agent_name text,
    p_greeting text,
    p_call_instructions text
) returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
    previous public.agent_configs%rowtype;
    instructions text;
begin
    select * into previous
    from public.agent_configs
    where organization_id = p_organization_id
    for update;
    if not found then
        return false;
    end if;
    instructions := coalesce(p_call_instructions, previous.call_instructions);
    if (previous.agent_name, previous.greeting, previous.call_instructions)
        is not distinct from (p_agent_name, p_greeting, instructions) then
        return true;
    end if;
    insert into public.agent_configuration_history (organization_id, agent_name, greeting, call_instructions)
    values (p_organization_id, previous.agent_name, previous.greeting, previous.call_instructions);
    update public.agent_configs
    set agent_name = p_agent_name, greeting = p_greeting, call_instructions = instructions
    where organization_id = p_organization_id;
    return true;
end;
$$;

revoke all on function public.save_agent_configuration(uuid, text, text, text) from public, anon, authenticated;
grant execute on function public.save_agent_configuration(uuid, text, text, text) to service_role;
