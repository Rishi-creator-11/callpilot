create table public.workspace_call_metrics (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete cascade,
    label text not null check (char_length(label) between 2 and 50),
    stage text check (stage in ('hot','warm','general','not_qualified','needs_review')),
    direction text check (direction in ('inbound','outbound','other')),
    min_seconds integer check (min_seconds between 1 and 14400),
    open_follow_up boolean not null default false
);
create unique index workspace_call_metrics_label_idx on public.workspace_call_metrics (organization_id, lower(label));
alter table public.workspace_call_metrics enable row level security;
revoke all on public.workspace_call_metrics from public, anon, authenticated;
grant select, insert, delete on public.workspace_call_metrics to service_role;

create function public.add_workspace_call_metric(p_workspace uuid, p_label text, p_stage text,
    p_direction text, p_min_seconds integer, p_open_follow_up boolean)
returns text language plpgsql security invoker set search_path = '' as $$
begin
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(p_workspace::text, 367210));
    if exists (select 1 from public.workspace_call_metrics
               where organization_id = p_workspace and lower(label) = lower(p_label)) then
        return 'duplicate';
    end if;
    if (select count(*) from public.workspace_call_metrics where organization_id = p_workspace) >= 8 then
        return 'limit';
    end if;
    insert into public.workspace_call_metrics (organization_id, label, stage, direction, min_seconds, open_follow_up)
    values (p_workspace, p_label, p_stage, p_direction, p_min_seconds, p_open_follow_up);
    return 'created';
end;
$$;
revoke all on function public.add_workspace_call_metric(uuid,text,text,text,integer,boolean) from public, anon, authenticated;
grant execute on function public.add_workspace_call_metric(uuid,text,text,text,integer,boolean) to service_role;

create or replace function public.workspace_call_analytics(p_workspace uuid, p_start date, p_end date)
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
               nullif(c.caller_phone, '') as caller, coalesce(c.lead_stage, 'needs_review') as lead_stage, c.direction,
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
    ),
    metrics as (
        select m.id, m.label, m.stage, m.direction, m.min_seconds, m.open_follow_up,
               count(a.day) as calls
        from public.workspace_call_metrics m
        left join activity a
          on (m.stage is null or m.stage = a.lead_stage)
         and (m.direction is null or m.direction = a.direction)
         and (m.min_seconds is null or a.duration_seconds >= m.min_seconds)
         and (not m.open_follow_up or a.open_follow_up)
        where m.organization_id = p_workspace
        group by m.id
    )
    select jsonb_build_object(
        'time_zone', (select zone from settings),
        'total', count(*),
        'unique_callers', count(distinct caller),
        'average_duration_seconds', round(avg(duration_seconds)),
        'follow_ups', count(*) filter (where open_follow_up),
        'inbound', count(*) filter (where direction = 'inbound'),
        'metrics', (select coalesce(jsonb_agg(to_jsonb(m) order by m.label), '[]'::jsonb) from metrics m),
        'daily', (select coalesce(jsonb_agg(to_jsonb(d) order by d.day), '[]'::jsonb) from daily d),
        'stages', (select coalesce(jsonb_agg(to_jsonb(s) order by s.calls desc, s.stage), '[]'::jsonb) from stages s)
    )
    from activity;
$$;

revoke all on function public.workspace_call_analytics(uuid, date, date) from public, anon, authenticated;
grant execute on function public.workspace_call_analytics(uuid, date, date) to service_role;
