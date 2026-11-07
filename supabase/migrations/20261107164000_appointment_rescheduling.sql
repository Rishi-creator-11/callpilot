alter table public.appointment_requests
    add column move_id uuid,
    add column move_start timestamptz,
    add column move_minutes integer check (move_minutes between 15 and 120),
    add column move_expires_at timestamptz;

create function public.hold_appointment_move(
    p_workspace uuid, p_appointment uuid, p_operation uuid, p_start timestamptz, p_minutes integer
) returns text language plpgsql security invoker set search_path = '' as $$
declare existing public.appointment_requests%rowtype;
begin
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(p_workspace::text, 17283));
    select * into existing from public.appointment_requests
    where id = p_appointment and organization_id = p_workspace for update;
    if not found or existing.status <> 'confirmed' or existing.calendar_event_id is null then return 'missing'; end if;
    if existing.move_id = p_operation and existing.move_expires_at is null then
        return case when existing.requested_time = p_start and existing.duration_minutes = p_minutes
                    then 'completed' else 'changed' end;
    end if;
    if existing.move_id = p_operation and (existing.move_start <> p_start or existing.move_minutes <> p_minutes)
        then return 'changed'; end if;
    if existing.move_start is not null and existing.move_id <> p_operation then return 'busy'; end if;
    if existing.move_expires_at > now() then
        return case when existing.move_id = p_operation then 'held' else 'busy' end;
    end if;
    if exists (
        select 1 from public.appointment_requests a where a.organization_id=p_workspace and a.id<>p_appointment
        and (
            (a.status in ('requested','confirmed') and
             pg_catalog.tstzrange(a.requested_time,a.requested_time+(a.duration_minutes * interval '1 minute'),'[)')
             && pg_catalog.tstzrange(p_start,p_start+(p_minutes * interval '1 minute'),'[)'))
            or
            (a.status='confirmed' and a.move_expires_at > now() and
             pg_catalog.tstzrange(a.move_start,a.move_start+(a.move_minutes * interval '1 minute'),'[)')
             && pg_catalog.tstzrange(p_start,p_start+(p_minutes * interval '1 minute'),'[)'))
        )
    ) then return 'conflict'; end if;
    update public.appointment_requests
    set move_id=p_operation, move_start=p_start, move_minutes=p_minutes, move_expires_at=now()+interval '5 minutes'
    where id=p_appointment;
    return 'held';
end;
$$;

create function public.finish_appointment_move(p_workspace uuid,p_appointment uuid,p_operation uuid,p_applied boolean)
returns boolean language plpgsql security invoker set search_path = '' as $$
begin
    update public.appointment_requests
    set requested_time=case when p_applied then move_start else requested_time end,
        duration_minutes=case when p_applied then move_minutes else duration_minutes end,
        move_id=case when p_applied then move_id else null end,
        move_start=null,move_minutes=null,move_expires_at=null
    where organization_id=p_workspace and id=p_appointment and move_id=p_operation
      and status='confirmed' and move_start is not null and move_minutes is not null;
    return found or exists (select 1 from public.appointment_requests
        where organization_id=p_workspace and id=p_appointment and move_id=p_operation
          and p_applied and status='confirmed' and move_start is null);
end;
$$;

revoke all on function public.hold_appointment_move(uuid,uuid,uuid,timestamptz,integer) from public,anon,authenticated;
revoke all on function public.finish_appointment_move(uuid,uuid,uuid,boolean) from public,anon,authenticated;
grant execute on function public.hold_appointment_move(uuid,uuid,uuid,timestamptz,integer) to service_role;
grant execute on function public.finish_appointment_move(uuid,uuid,uuid,boolean) to service_role;

create or replace function public.reserve_voice_appointment(p_organization_id uuid, p_idempotency_key text,
  p_caller_name text, p_caller_contact text, p_service text, p_requested_time timestamptz,
  p_duration_minutes integer, p_provider_call_id text)
returns table(reservation_status text,reservation_id uuid)
language plpgsql security invoker set search_path = '' as $$
declare v_existing public.appointment_requests%rowtype; v_id uuid;
begin
    if p_organization_id is null or length(p_idempotency_key) <> 64
       or p_duration_minutes not between 15 and 120 or p_requested_time < now()
       or p_requested_time > now() + interval '180 days'
       or length(trim(p_caller_name)) not between 2 and 100
       or length(trim(p_caller_contact)) not between 3 and 100
       or length(trim(p_service)) not between 2 and 150
       or length(trim(p_provider_call_id)) not between 4 and 150 then
        raise exception 'Invalid booking details' using errcode='22023';
    end if;
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(p_organization_id::text,17283));
    select * into v_existing from public.appointment_requests
    where organization_id=p_organization_id and idempotency_key=p_idempotency_key limit 1;
    if found then
        return query select v_existing.status::text,v_existing.id;
        return;
    end if;
    if exists (select 1 from public.appointment_requests a where a.organization_id=p_organization_id and (
        (a.status in ('requested','confirmed') and
         pg_catalog.tstzrange(a.requested_time,a.requested_time+(a.duration_minutes * interval '1 minute'),'[)')
         && pg_catalog.tstzrange(p_requested_time,p_requested_time+(p_duration_minutes * interval '1 minute'),'[)'))
        or (a.status='confirmed' and a.move_expires_at>now() and
         pg_catalog.tstzrange(a.move_start,a.move_start+(a.move_minutes * interval '1 minute'),'[)')
         && pg_catalog.tstzrange(p_requested_time,p_requested_time+(p_duration_minutes * interval '1 minute'),'[)'))
    )) then
        return query select 'conflict'::text,null::uuid;
        return;
    end if;
    insert into public.appointment_requests
        (organization_id,idempotency_key,caller_name,caller_contact,service,requested_time,duration_minutes,provider_call_id,status)
    values (p_organization_id,p_idempotency_key,trim(p_caller_name),trim(p_caller_contact),trim(p_service),
            p_requested_time,p_duration_minutes,trim(p_provider_call_id),'requested')
    returning id into v_id;
    return query select 'reserved'::text,v_id;
end;
$$;
