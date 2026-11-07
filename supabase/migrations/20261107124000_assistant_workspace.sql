create table public.builder_conversations (
    organization_id uuid not null references public.organizations(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    id uuid not null default gen_random_uuid(),
    turns jsonb not null default '[]'::jsonb,
    primary key (organization_id, user_id),
    check (jsonb_typeof(turns) = 'array' and jsonb_array_length(turns) <= 30)
);

alter table public.builder_conversations enable row level security;
revoke all on public.builder_conversations from public, anon, authenticated;
grant select, insert, update on public.builder_conversations to service_role;

create function public.append_builder_turn(p_workspace uuid, p_user uuid, p_conversation uuid, p_turn jsonb)
returns boolean language plpgsql security invoker set search_path = '' as $$
begin
    update public.builder_conversations
    set turns = case when jsonb_array_length(turns) >= 30 then turns - 0 else turns end
        || jsonb_build_array(p_turn)
    where organization_id = p_workspace and user_id = p_user and id = p_conversation;
    return found;
end;
$$;

revoke all on function public.append_builder_turn(uuid, uuid, uuid, jsonb) from public, anon, authenticated;
grant execute on function public.append_builder_turn(uuid, uuid, uuid, jsonb) to service_role;

create index if not exists call_insights_org_started_at_idx
    on public.call_insights (organization_id, started_at desc);

create function public.workspace_call_analytics(p_workspace uuid, p_start date, p_end date)
returns jsonb language sql stable security invoker set search_path = '' as $$
    with settings as (
        select coalesce(
            (select tz.name from public.organizations o join pg_catalog.pg_timezone_names tz on tz.name = o.timezone
             where o.id = p_workspace),
            'UTC'
        ) as zone
    ),
    activity as (
        select (c.started_at at time zone s.zone)::date as day, c.duration_seconds,
               nullif(c.caller_phone, '') as caller, c.lead_stage, c.direction,
               coalesce(c.follow_up_needed and c.follow_up_status is distinct from 'done', false) as open_follow_up
        from public.call_insights c cross join settings s
        where c.organization_id = p_workspace
          and c.started_at >= (p_start::timestamp at time zone s.zone)
          and c.started_at < ((p_end + 1)::timestamp at time zone s.zone)
    ),
    daily as (
        select p_start + dates.n as day, coalesce(counts.calls, 0) as calls
        from pg_catalog.generate_series(0, p_end - p_start) as dates(n)
        left join (select day, count(*) as calls from activity group by day) counts
            on counts.day = p_start + dates.n
    ),
    stages as (
        select coalesce(lead_stage, 'needs_review') as stage, count(*) as calls
        from activity group by coalesce(lead_stage, 'needs_review')
    )
    select jsonb_build_object(
        'time_zone', (select zone from settings),
        'total', count(*),
        'unique_callers', count(distinct caller),
        'average_duration_seconds', round(avg(duration_seconds)),
        'follow_ups', count(*) filter (where open_follow_up),
        'inbound', count(*) filter (where direction = 'inbound'),
        'daily', (select coalesce(jsonb_agg(to_jsonb(d) order by d.day), '[]'::jsonb) from daily d),
        'stages', (select coalesce(jsonb_agg(to_jsonb(s) order by s.calls desc, s.stage), '[]'::jsonb) from stages s)
    )
    from activity;
$$;

revoke all on function public.workspace_call_analytics(uuid, date, date) from public, anon, authenticated;
grant execute on function public.workspace_call_analytics(uuid, date, date) to service_role;
