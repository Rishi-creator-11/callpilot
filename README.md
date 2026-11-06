# CallPilot — Multi-business AI Receptionist Platform

## Current state
Internal preproduction preview deployed to Render. Dedicated Supabase database has been created and migrations applied. No customer-facing calls, verified custom voices, or confirmed calendar bookings are active.

- Live console: https://callpilot-internal.onrender.com
- Supabase project: https://supabase.com/dashboard/project/ftltlmigdokfzqazrsrt
- Render service: https://dashboard.render.com/web/srv-dbmkk03m8hqs73ao29s0

## Security configuration (Render → Environment)
Set the following values in **Render server-side environment variables**. Never send private keys in chat, paste them into browser-side code, or commit them to this GitHub repo.

| Variable | Purpose |
| --- | --- |
| ADMIN_TOKEN | Strong random string, authorizes the internal operator console |
| SUPABASE_URL | https://ftltlmigdokfzqazrsrt.supabase.co (already configured in Render) |
| SUPABASE_SECRET_KEY | Copy `sb_secret_...` from Supabase project Settings → API Keys; bypasses RLS and is server-only |
| VAPI_API_KEY | Vapi private API key, used only on backend to create the TEST assistant |
| VAPI_WEBHOOK_TOKEN | Optional, random secret for authenticated event webhooks |
| VAPI_CREDENTIAL_ID | Optional Vapi server credential ID; configure same bearer token as VAPI_WEBHOOK_TOKEN |
| PUBLIC_BASE_URL | Optional https://callpilot-internal.onrender.com |

### Where to find the key
Supabase project dashboard → Settings → API Keys → Secret keys. If no modern secret key exists, create one there. Only use a *secret* key on the backend, not a publishable key.

Vapi dashboard → API Keys → Private key. Paste into Render as `VAPI_API_KEY` and save. This is not a GitHub credential and does **not** belong in a repository secret unless CI needs it.

### How to test
1. Open the console URL and enter ADMIN_TOKEN.
2. Click Connect; it should report persistent storage configured.
3. Create a test business and add one approved business FAQ. Click Approve on its pending answer.
4. In policy simulator ask that precise question; the approved answer should appear.
5. Ask "How much does this cost?" and confirm the answer refuses to quote.
6. When VAPI_API_KEY is configured, select the business and click Create Vapi test assistant. Inspect resulting assistant ID in Vapi dashboard.
7. Do not assign a live phone number until voice prompt behavior, caller disclosure, accuracy, recording law and human fallback have been reviewed.

## Architecture
- FastAPI backend; Render hosting, protected by ADMIN_TOKEN for internal demo
- Supabase Postgres (separate CallPilot project), seven tenant-scoped core tables, RLS enabled and public table grants revoked
- Server-only privileged PostgREST adapter in `app/cloud.py`; no secret keys sent to browser
- `app/vapi.py` receives authenticated end-of-call events and stores **metadata only**, not recordings
- `static/cloud.html` is the persistent operator console
- Legacy `static/index.html` and SQLite demo remain for local development but are **not persistent on Render**
- Vapi test assistant creator uses approved FAQs. It has NO booking tools; therefore it must never say it booked an appointment.
- Custom voice cannot be provisioned until verified voice-owner consent is recorded. A checkbox alone does not authorize cloning.

## Important remaining production work
Implement Supabase Auth and tenant-aware authorization replacing the single shared admin token, robust booking/calendar tool with idempotency, signed OAuth integrations, voice consent-verification workflow, provider tool-call validation, contact routing, customer-specific knowledge ingestion, policy-validation testing, observability and retention policies. No guarantee of zero hallucinations is possible for a generative agent; live calls must be tested and supervised before customer launch. Outbound calls remain disabled.

## Local run
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export ADMIN_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn app.main:app --reload
```

Python 3.11 is pinned for Render.

## Customer login (new)
Public customer app: https://callpilot-internal.onrender.com/app
Internal admin console moved to: https://callpilot-internal.onrender.com/operator
The customer UI uses Supabase Auth, verified-email JWTs and server-enforced organization membership. No shared admin token is used by customer APIs.

Supabase Authentication → URL Configuration:
- Site URL: `https://callpilot-internal.onrender.com`
- Redirect allowlist: `https://callpilot-internal.onrender.com/app`

Supabase hosted projects require verified email. The built-in SMTP sender is limited to organization team email addresses and a tiny rate limit; configure a production SMTP provider before public signup. Never disable email verification merely to bypass email setup.

## Stripe billing
A signed webhook and hosted Checkout/Customer Portal integration are in `app/billing.py`. It is deliberately disabled until subscription prices have been explicitly approved and Stripe test credentials are configured. Only `sk_test_...` keys are accepted; live charges cannot be initiated by this code path.
- `STRIPE_BILLING_ENABLED=false` (configured on Render)
- Later test configuration: `STRIPE_MODE=test`, `STRIPE_SECRET_KEY=sk_test_...`, `STRIPE_WEBHOOK_SECRET=whsec_...`, `STRIPE_PRICE_ID=price_...`
- Webhook endpoint: `https://callpilot-internal.onrender.com/webhooks/stripe`
- Events: `customer.subscription.created`, `.updated`, `.deleted`
- After price approval and sandbox testing, review paid entitlements, price validation and cancellation scenarios before any live billing.

## Calendar integration
OAuth start and callback plus live availability and internal test bookings are implemented but disabled until Google OAuth configuration:
- `GOOGLE_OAUTH_CLIENT_ID`
- `GOOGLE_OAUTH_CLIENT_SECRET`
- `GOOGLE_TOKEN_ENCRYPTION_KEY`: generate using `python3 -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`
- Authorized Google OAuth redirect URI: `https://callpilot-internal.onrender.com/api/oauth/google/callback`
- Calendar API enabled in Google Cloud project; consent screen configured for test users.
- User grants calendar.events and calendar.events.freebusy scopes.
This is a pilot booking flow. Concurrency handling, retries/reconciliation, consent and business-hour rules must be hardened before letting autonomous voice agents book customers.

## Verified owner voice
Customer app can submit a custom-voice request with an ElevenLabs shared voice ID. This request stays PENDING until verified by the provider; the UI checkbox acknowledges authorization but is **not** proof of identity and cannot activate voice cloning. Voice impersonation is not allowed.

## Still outstanding before a public launch
Real business email SMTP, domain and website terms, password reset and account recovery UX, billing sandbox integration and prices, Google OAuth keys, production voice-provider tool calls, phone number connection and inbound call QA, provider-verified voice activation, legal compliance/recording policy, webhook idempotency across failures, calendar reservation concurrency and conflict reconciliation, monitoring, consent flows, abuse protection, security review and pilot acceptance tests.
