alter table public.agent_configs
    add column call_menu jsonb not null default '{"enabled":false,"options":[]}'::jsonb,
    add column call_menu_revision uuid not null default gen_random_uuid(),
    add constraint agent_configs_call_menu_object check (jsonb_typeof(call_menu)='object');
