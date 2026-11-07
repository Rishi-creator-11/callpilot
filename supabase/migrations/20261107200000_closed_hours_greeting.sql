-- Optional greeting used when a call arrives outside the workspace's configured open hours.
alter table public.agent_configs
    add column if not exists closed_greeting text
    check (closed_greeting is null or char_length(closed_greeting) between 10 and 500);
