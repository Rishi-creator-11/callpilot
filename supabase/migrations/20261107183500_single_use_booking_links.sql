create table public.appointment_booking_links (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete cascade,
    service text not null check (length(service) between 2 and 150),
    duration_minutes integer not null check (duration_minutes between 15 and 120),
    status text not null default 'open' check (status in ('open','pending','booked','revoked')),
    created_at timestamptz not null default now(),
    expires_at timestamptz not null default (now()+interval '7 days'),
    checks integer not null default 0 check (checks between 0 and 24),
    attempts integer not null default 0 check (attempts between 0 and 6),
    claims integer not null default 0 check (claims between 0 and 12),
    request_hash text,
    request_id uuid,
    lease_until timestamptz,
    appointment_id uuid references public.appointment_requests(id) on delete set null,
    check (status<>'pending' or (request_hash is not null and request_id is not null))
);
create index appointment_booking_links_recent
    on public.appointment_booking_links(organization_id,created_at desc);
alter table public.appointment_booking_links enable row level security;
revoke all on public.appointment_booking_links from public,anon,authenticated;
grant select,insert,update on public.appointment_booking_links to service_role;

create function public.issue_appointment_booking_link(p_workspace uuid,p_service text,p_duration integer)
returns uuid language plpgsql security invoker set search_path = '' as $$
declare link_id uuid;
begin
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(p_workspace::text,31894));
    if (select count(*) from public.appointment_booking_links where organization_id=p_workspace
        and status in ('open','pending') and expires_at>now())>=5
        or (select count(*) from public.appointment_booking_links where organization_id=p_workspace
        and created_at>now()-interval '24 hours')>=10 then return null; end if;
    insert into public.appointment_booking_links(organization_id,service,duration_minutes)
    values(p_workspace,p_service,p_duration) returning id into link_id;
    return link_id;
end;
$$;

create function public.claim_appointment_booking_link(p_link uuid,p_workspace uuid,p_hash text)
returns uuid language plpgsql security invoker set search_path = '' as $$
declare link public.appointment_booking_links%rowtype;
begin
    select * into link from public.appointment_booking_links
    where id=p_link and organization_id=p_workspace and expires_at>now() for update;
    if not found or link.claims>=12 then return null; end if;
    if link.status='pending' and link.request_hash=p_hash then
        if link.lease_until>now() then return null; end if;
        update public.appointment_booking_links set lease_until=now()+interval '2 minutes',claims=claims+1 where id=p_link;
        return link.request_id;
    end if;
    if link.status<>'open' or link.attempts>=6 then return null; end if;
    update public.appointment_booking_links
    set status='pending',request_hash=p_hash,request_id=gen_random_uuid(),attempts=attempts+1,
        lease_until=now()+interval '2 minutes',claims=claims+1
    where id=p_link returning request_id into link.request_id;
    return link.request_id;
end;
$$;
revoke all on function public.issue_appointment_booking_link(uuid,text,integer) from public,anon,authenticated;
revoke all on function public.claim_appointment_booking_link(uuid,uuid,text) from public,anon,authenticated;
grant execute on function public.issue_appointment_booking_link(uuid,text,integer) to service_role;
grant execute on function public.claim_appointment_booking_link(uuid,uuid,text) to service_role;
