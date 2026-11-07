alter table public.agent_configs
    add column call_instructions text not null default ''
    check (char_length(call_instructions) <= 2400);
