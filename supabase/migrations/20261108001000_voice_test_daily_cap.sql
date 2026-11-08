-- Same limits as before (one call per 3 minutes and five per day per workspace or user),
-- with the shared daily cap supplied by the app so growth does not silently block everyone.
create function public.reserve_voice_preview(p_organization_id uuid, p_user_id uuid, p_daily_cap integer)
returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
    day_start timestamptz := date_trunc('day', now(), 'UTC');
begin
    perform pg_advisory_xact_lock(1728456941);

    if exists (
        select 1 from public.voice_preview_attempts
        where created_at >= now() - interval '3 minutes'
          and (organization_id = p_organization_id or user_id = p_user_id)
    ) or (
        select count(*) from public.voice_preview_attempts
        where created_at >= day_start
          and (organization_id = p_organization_id or user_id = p_user_id)
    ) >= 5 or (
        select count(*) from public.voice_preview_attempts where created_at >= day_start
    ) >= greatest(1, least(p_daily_cap, 5000)) then
        return false;
    end if;

    insert into public.voice_preview_attempts (organization_id, user_id)
        values (p_organization_id, p_user_id);
    return true;
end;
$$;

revoke all on function public.reserve_voice_preview(uuid, uuid, integer) from public, anon, authenticated;
grant execute on function public.reserve_voice_preview(uuid, uuid, integer) to service_role;
