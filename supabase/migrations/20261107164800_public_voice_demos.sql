alter table public.workspace_demo_links
    add column voice_enabled boolean not null default false,
    add column voice_count integer not null default 0;

create function public.create_workspace_voice_demo(p_workspace uuid,p_label text)
returns uuid language plpgsql security invoker set search_path = '' as $$
declare result uuid;
begin
    result := public.create_workspace_demo(p_workspace,p_label);
    if result is not null then
        update public.workspace_demo_links set voice_enabled=true where id=result;
    end if;
    return result;
end;
$$;
revoke all on function public.create_workspace_voice_demo(uuid,text) from public,anon,authenticated;
grant execute on function public.create_workspace_voice_demo(uuid,text) to service_role;

create table public.public_demo_voice_attempts (
    id bigint generated always as identity primary key,
    link_id uuid not null references public.workspace_demo_links(id) on delete cascade,
    organization_id uuid not null references public.organizations(id) on delete cascade,
    started_at timestamptz not null default now()
);
create index public_demo_voice_recent_idx on public.public_demo_voice_attempts (started_at);
alter table public.public_demo_voice_attempts enable row level security;
revoke all on public.public_demo_voice_attempts from public,anon,authenticated;
grant select,insert,delete on public.public_demo_voice_attempts to service_role;
grant usage on sequence public.public_demo_voice_attempts_id_seq to service_role;

create function public.reserve_public_demo_voice(p_link uuid,p_workspace uuid)
returns boolean language plpgsql security invoker set search_path = '' as $$
declare day_boundary timestamptz := date_trunc('day',now(),'UTC');
begin
    perform pg_catalog.pg_advisory_xact_lock(75649231);
    if not exists (select 1 from public.workspace_demo_links
        where id=p_link and organization_id=p_workspace and voice_enabled and revoked_at is null and expires_at>now()
        for update) then return false; end if;
    if exists (select 1 from public.public_demo_voice_attempts
        where organization_id=p_workspace and started_at>now()-interval '1 minute')
        or (select count(*) from public.public_demo_voice_attempts where started_at>=day_boundary and link_id=p_link)>=2
        or (select count(*) from public.public_demo_voice_attempts where started_at>=day_boundary and organization_id=p_workspace)>=6
        or (select count(*) from public.public_demo_voice_attempts where started_at>=day_boundary)>=30
        then return false; end if;
    delete from public.public_demo_voice_attempts where started_at<day_boundary-interval '2 days';
    insert into public.public_demo_voice_attempts (link_id,organization_id) values (p_link,p_workspace);
    update public.workspace_demo_links set voice_count=voice_count+1 where id=p_link;
    return true;
end;
$$;
revoke all on function public.reserve_public_demo_voice(uuid,uuid) from public,anon,authenticated;
grant execute on function public.reserve_public_demo_voice(uuid,uuid) to service_role;
