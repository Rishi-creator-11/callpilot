-- Public enterprise inquiries. Written only by the server; never readable by app users.
create table public.enterprise_inquiries (
    id uuid primary key default gen_random_uuid(),
    name text not null check (char_length(name) between 2 and 100),
    email text not null check (char_length(email) between 5 and 200),
    company text not null check (char_length(company) between 2 and 120),
    phone text check (phone is null or char_length(phone) <= 40),
    monthly_calls text check (monthly_calls is null or char_length(monthly_calls) <= 40),
    message text check (message is null or char_length(message) <= 2000),
    created_at timestamptz not null default now()
);
alter table public.enterprise_inquiries enable row level security;
revoke all on public.enterprise_inquiries from public, anon, authenticated;
grant select, insert on public.enterprise_inquiries to service_role;
