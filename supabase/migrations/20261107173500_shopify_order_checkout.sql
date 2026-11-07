alter table public.order_requests
    add column checkout_state text check (checkout_state in ('pending','failed','created')),
    add column checkout_attempt uuid,
    add column checkout_shop text,
    add column checkout_draft_id text,
    add constraint order_requests_checkout_consistent check (
        (checkout_state is null and checkout_attempt is null and checkout_shop is null and checkout_draft_id is null)
        or (checkout_state in ('pending','failed') and checkout_attempt is not null and checkout_shop is not null and checkout_draft_id is null)
        or (checkout_state='created' and checkout_attempt is not null and checkout_shop is not null and checkout_draft_id is not null)
    );
