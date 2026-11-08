-- Hosted LobbyStack receptionist: one LobbyStack business per CallPilot workspace, plus a deduplicated event log.
create table if not exists lobbystack_businesses (
  organization_id uuid primary key references organizations(id) on delete cascade,
  business_id text not null unique,
  webhook_id text,
  webhook_secret_ciphertext text,
  webhook_verified_at timestamptz,
  calendar_status text not null default 'not_connected',
  created_at timestamptz not null default now()
);
create table if not exists lobbystack_events (
  event_id text primary key,
  organization_id uuid not null references organizations(id) on delete cascade,
  event_type text not null,
  resource_id text,
  payload jsonb not null,
  occurred_at timestamptz,
  received_at timestamptz not null default now()
);
create index if not exists lobbystack_events_org on lobbystack_events (organization_id, received_at desc);
alter table lobbystack_businesses enable row level security;
alter table lobbystack_events enable row level security;
