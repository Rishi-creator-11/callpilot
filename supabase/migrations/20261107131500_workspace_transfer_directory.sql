alter table public.agent_configs
    add column transfer_directory jsonb not null default '[]'::jsonb
    check (jsonb_typeof(transfer_directory) = 'array' and jsonb_array_length(transfer_directory) <= 8);

create function public.save_workspace_transfer_directory(p_workspace uuid, p_previous jsonb, p_directory jsonb)
returns boolean language plpgsql security invoker set search_path = '' as $$
begin
    update public.agent_configs
    set transfer_directory = p_directory
    where organization_id = p_workspace and transfer_directory = p_previous;
    return found;
end;
$$;

revoke all on function public.save_workspace_transfer_directory(uuid,jsonb,jsonb) from public, anon, authenticated;
grant execute on function public.save_workspace_transfer_directory(uuid,jsonb,jsonb) to service_role;
