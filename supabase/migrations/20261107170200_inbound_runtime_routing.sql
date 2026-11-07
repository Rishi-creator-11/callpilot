alter table public.agent_configs
    add column inbound_routing jsonb not null default '{"enabled":false,"runtime_workers":false,"rules":[]}'::jsonb
    check (jsonb_typeof(inbound_routing)='object' and jsonb_typeof(inbound_routing->'rules')='array'
           and jsonb_array_length(inbound_routing->'rules')<=12);

create table public.runtime_call_routes (
    organization_id uuid not null references public.organizations(id) on delete cascade,
    provider_call_id text not null,
    provider_phone_id text not null,
    route text not null,
    matched_rule text,
    created_at timestamptz not null default now(),
    primary key (organization_id,provider_call_id)
);
alter table public.runtime_call_routes enable row level security;
revoke all on public.runtime_call_routes from public,anon,authenticated;
grant select,insert on public.runtime_call_routes to service_role;

create function public.save_inbound_routing(p_workspace uuid,p_previous jsonb,p_routing jsonb)
returns boolean language plpgsql security invoker set search_path = '' as $$
begin
    update public.agent_configs set inbound_routing=p_routing
    where organization_id=p_workspace and inbound_routing=p_previous;
    return found;
end;
$$;
revoke all on function public.save_inbound_routing(uuid,jsonb,jsonb) from public,anon,authenticated;
grant execute on function public.save_inbound_routing(uuid,jsonb,jsonb) to service_role;
