create table public.workspace_invitations (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete cascade,
    email text not null check (char_length(email) between 3 and 254 and email = lower(email)),
    role text not null check (role in ('admin', 'viewer')),
    token_hash text not null unique check (token_hash ~ '^[0-9a-f]{64}$'),
    issued_by uuid not null,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    revoked_at timestamptz,
    accepted_at timestamptz,
    accepted_by uuid,
    unique (organization_id, email, role)
);

alter table public.workspace_invitations enable row level security;
revoke all on public.workspace_invitations from public, anon, authenticated;
grant select, insert, update on public.workspace_invitations to service_role;

create function public.accept_workspace_invitation(p_hash text, p_user_id uuid, p_email text)
returns uuid
language plpgsql
security invoker
set search_path = ''
as $$
declare
    invitation public.workspace_invitations%rowtype;
begin
    select * into invitation from public.workspace_invitations
        where token_hash = p_hash for update;
    if not found or invitation.email <> lower(p_email) then
        return null;
    end if;
    if invitation.accepted_by = p_user_id and exists (
        select 1 from public.organization_members m
        where m.organization_id = invitation.organization_id and m.user_id = p_user_id
    ) then
        return invitation.organization_id;
    end if;
    if invitation.revoked_at is not null or invitation.accepted_at is not null
        or invitation.expires_at <= now() or not exists (
            select 1 from public.organization_members m
            where m.organization_id = invitation.organization_id and m.user_id = invitation.issued_by
                and (m.role = 'owner' or (m.role = 'admin' and invitation.role = 'viewer'))
        ) then
        return null;
    end if;

    insert into public.organization_members (organization_id, user_id, role)
        values (invitation.organization_id, p_user_id, invitation.role)
        on conflict (organization_id, user_id) do nothing;

    update public.workspace_invitations
        set accepted_at = now(), accepted_by = p_user_id where id = invitation.id;
    return invitation.organization_id;
end;
$$;

revoke all on function public.accept_workspace_invitation(text, uuid, text) from public, anon, authenticated;
grant execute on function public.accept_workspace_invitation(text, uuid, text) to service_role;
