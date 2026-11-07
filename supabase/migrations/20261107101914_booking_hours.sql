alter table public.organizations
    add column booking_hours jsonb
    check (booking_hours is null or jsonb_typeof(booking_hours) = 'object');
