# CallPilot — AI Receptionist Studio

Protected engineering demo for a multi-business AI receptionist and appointment workflow. **No selling prices are configured.** This repository contains no production credentials.

## Features
- Create separate business workspaces and approved knowledge bases.
- Configure standard voice agent metadata; owner voice requires verified owner consent and a provider voice ID (future live integration).
- Simulate conservative responses with explicit no-price policy and escalation for unknown questions.
- Record appointment *requests* without claiming bookings are confirmed.
- Fail-closed operator API protected by `ADMIN_TOKEN`.

## Run locally
```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn app.main:app --reload
```
Visit http://localhost:8000 and enter your ADMIN_TOKEN. For a cloud preview, set a random ADMIN_TOKEN in server-side Render environment settings; never commit it or a Vapi private key. A preview on Render's ephemeral filesystem will lose SQLite records on redeploy.

## Production blockers
This application is **not yet a live phone agent**. Live telephony, signed Vapi webhooks, real calendar availability and confirmation, customer login/RLS, persistent Supabase Postgres, verified voice cloning, recording disclosures, robust policy enforcement, monitoring and billing are not yet implemented. Do not enter real customer information or expose it as customer-ready SaaS. The no-price simulator is not a guarantee about any external LLM.

## Next step
Deploy protected internal preview, provision a dedicated Supabase project, wire Vapi server-side with secure keys, and add calendar auth and idempotent booking transactions. Do not activate outbound calling by default.
