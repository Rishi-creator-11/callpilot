create table public.phone_provision_requests (
    organization_id uuid primary key references public.organizations(id) on delete cascade,
    request_id uuid not null,
    area_code text not null check (area_code ~ '^[2-9][0-9]{2}$'),
    status text not null check (status in ('pending','failed','provisioned')),
    provider_phone_id text,
    updated_at timestamptz not null default now()
);
alter table public.phone_provision_requests enable row level security;
revoke all on public.phone_provision_requests from public,anon,authenticated;
grant select,insert,update on public.phone_provision_requests to service_role;

create function public.begin_managed_phone_provision(p_workspace uuid,p_request uuid,p_area text)
returns boolean language plpgsql security invoker set search_path = '' as $$
begin
    if not exists(select 1 from public.agent_configs where organization_id=p_workspace
        and provider_phone_id is null and provider_agent_id is not null) then return false; end if;
    insert into public.phone_provision_requests(organization_id,request_id,area_code,status)
    values(p_workspace,p_request,p_area,'pending')
    on conflict(organization_id) do update
    set request_id=excluded.request_id,area_code=excluded.area_code,status='pending',
        provider_phone_id=null,updated_at=now()
    where public.phone_provision_requests.status='failed';
    return found;
end;
$$;

create function public.finish_managed_phone_provision(p_workspace uuid,p_request uuid,p_phone_id text,p_number text)
returns boolean language plpgsql security invoker set search_path = '' as $$
declare current_phone text;
begin
    perform 1 from public.phone_provision_requests
    where organization_id=p_workspace and request_id=p_request and status in ('pending','provisioned')
    for update;
    if not found then return false; end if;
    select provider_phone_id into current_phone from public.agent_configs
    where organization_id=p_workspace for update;
    if not found or (current_phone is not null and current_phone<>p_phone_id) then return false; end if;
    if current_phone is null then
        insert into public.phone_assignments(organization_id,provider_phone_id,provider_number,status)
        values(p_workspace,p_phone_id,p_number,'pending');
        update public.agent_configs set provider_phone_id=p_phone_id where organization_id=p_workspace;
    end if;
    update public.phone_provision_requests set provider_phone_id=p_phone_id,status='provisioned',updated_at=now()
    where organization_id=p_workspace and request_id=p_request;
    return true;
end;
$$;

revoke all on function public.begin_managed_phone_provision(uuid,uuid,text) from public,anon,authenticated;
revoke all on function public.finish_managed_phone_provision(uuid,uuid,text,text) from public,anon,authenticated;
grant execute on function public.begin_managed_phone_provision(uuid,uuid,text) to service_role;
grant execute on function public.finish_managed_phone_provision(uuid,uuid,text,text) to service_role;

create function public.activate_managed_phone_assignment(p_workspace uuid,p_phone_id text)
returns boolean language plpgsql security invoker set search_path = '' as $$
begin
    perform 1 from public.phone_assignments
    where organization_id=p_workspace and provider_phone_id=p_phone_id
        and status in ('pending','linked') for update;
    if not found then return false; end if;
    update public.billing_accounts set enforcement_enabled=true
    where organization_id=p_workspace and status in ('active','trialing') and setup_paid_at is not null;
    if not found then return false; end if;
    update public.phone_assignments set status='linked'
    where organization_id=p_workspace and provider_phone_id=p_phone_id and status='pending';
    return true;
end;
$$;
revoke all on function public.activate_managed_phone_assignment(uuid,text) from public,anon,authenticated;
grant execute on function public.activate_managed_phone_assignment(uuid,text) to service_role;
