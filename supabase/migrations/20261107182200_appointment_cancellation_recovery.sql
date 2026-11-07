alter table public.appointment_requests
    add column cancel_pending_ref text check (cancel_pending_ref ~ '^[a-f0-9]{64}$');

create or replace function public.hold_appointment_move(
    p_workspace uuid, p_appointment uuid, p_operation uuid, p_start timestamptz, p_minutes integer
) returns text language plpgsql security invoker set search_path = '' as $$
declare existing public.appointment_requests%rowtype;
begin
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(p_workspace::text, 17283));
    select * into existing from public.appointment_requests
    where id = p_appointment and organization_id = p_workspace for update;
    if not found or existing.status <> 'confirmed' or existing.calendar_event_id is null then return 'missing'; end if;
    if existing.cancel_pending_ref is not null then return 'busy'; end if;
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
