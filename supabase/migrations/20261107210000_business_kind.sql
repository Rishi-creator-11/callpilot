-- What the business does, so the agent can behave like a products desk or a services front desk.
alter table public.agent_configs
    add column if not exists business_kind text check (business_kind is null or business_kind in ('products', 'services')),
    add column if not exists business_type text check (business_type is null or business_type ~ '^[a-z_]{2,30}$');
