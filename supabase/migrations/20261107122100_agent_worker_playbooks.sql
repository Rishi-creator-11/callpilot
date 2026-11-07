alter table public.agent_configs
    add column worker_playbooks jsonb not null default '[]'::jsonb
    check (jsonb_typeof(worker_playbooks) = 'array' and jsonb_array_length(worker_playbooks) <= 12);

create function public.save_worker_playbooks(p_workspace uuid, p_previous jsonb, p_playbooks jsonb)
returns boolean language plpgsql security invoker set search_path = '' as $$
begin
    update public.agent_configs
    set worker_playbooks = p_playbooks
    where organization_id = p_workspace and worker_playbooks = p_previous;
    return found;
end;
$$;

revoke all on function public.save_worker_playbooks(uuid, jsonb, jsonb) from public, anon, authenticated;
grant execute on function public.save_worker_playbooks(uuid, jsonb, jsonb) to service_role;
