create table public.voice_preview_attempts (
    organization_id uuid not null references public.organizations(id) on delete cascade,
    user_id uuid not null,
    created_at timestamptz not null default now()
);

create index voice_preview_attempts_created_idx
    on public.voice_preview_attempts (created_at);

alter table public.voice_preview_attempts enable row level security;
revoke all on public.voice_preview_attempts from public, anon, authenticated;
grant select, insert, delete on public.voice_preview_attempts to service_role;

create function public.reserve_voice_preview(p_organization_id uuid, p_user_id uuid)
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
    ) >= 40 then
        return false;
    end if;

    insert into public.voice_preview_attempts (organization_id, user_id)
        values (p_organization_id, p_user_id);
    return true;
end;
$$;

revoke all on function public.reserve_voice_preview(uuid, uuid) from public, anon, authenticated;
grant execute on function public.reserve_voice_preview(uuid, uuid) to service_role;
