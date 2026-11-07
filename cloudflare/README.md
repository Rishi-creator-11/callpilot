# CallPilot Cloudflare migration — staging only

## Goal
Run CallPilot on the existing paid Cloudflare account without interrupting live calls.

## Deployed experiment
The `callpilot-staging` Python Worker exposes `/health` plus an explicit staging notice. It does **not** currently serve production accounts, recordings, Supabase APIs, Stripe, or Vapi. Do not configure any phone number or webhook to this staging URL.

## Deployment
GitHub Actions runs `uv run pywrangler deploy` from this directory. It uses the repository action secrets `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`.

## Remaining porting work
- Remove SQLite and mutable filesystem initialization from the production FastAPI application.
- Rework sync outbound HTTP requests and dependency packages for Python Workers.
- Port authentication, tenant isolation, call intelligence, recordings and Supabase features.
- Move existing provider secrets to Cloudflare **Worker secrets** (GitHub Actions secrets alone are not runtime secrets). No secret values in source control or chat.
- Move/verify Google OAuth callback domains, Supabase Auth site/redirect settings, Stripe success/cancel URLs, signed Stripe webhooks, and Vapi server URLs.
- Verify authenticated phone calls, failed webhooks, payments, usage enforcement, call recordings, login and mobile screens on staging.
- Perform cutover only after passed testing. Retain Render rollback until stable.
