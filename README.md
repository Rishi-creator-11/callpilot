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
