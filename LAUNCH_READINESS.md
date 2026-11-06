# CallPilot — Launch readiness audit (2026-11-06)

## Commercial offer agreed
- Starter: $49/month, 100 completed inbound AI call minutes (about 50 calls at two minutes each).
- Growth: $99/month, 300 minutes (about 150 estimated calls).
- Business: $179/month, 600 minutes (about 300 estimated calls).
- Mandatory $100 one-time professional setup per business, collected on initial subscription checkout only.
- No automatic overage purchases, no daily call cap.
- Every call is capped by Vapi at four minutes (240 seconds); a wrap-up cue at 215 seconds.
- Even with a hard cap, the last accepted call can exceed the remaining monthly allowance by up to four minutes. Disclose this operationally; never promise an exact zero-overage provider invoice.

## Current confirmed configuration
- Repo: Rishi-creator-11/callpilot (public at time of review — change to private prior to broader launch).
- Render service: callpilot-internal, Free tier, auto-deploy. Free tier sleeps on idle and is not production suitable.
- Supabase: real organization, knowledge and calls exist; RLS and server-only credentials.
- Vapi: existing assistant and first real call; provider confirmed four-minute maximum in prior startup log. Assistant linked.
- Business phone: CallPilot did not yet link phone assignment as of this audit; safe auto-discovery was committed and must be verified after deploy.
- Billing: sandbox historical state, organization subscription unconfigured; no production-paid customer.
- Stripe connected account: Jaivin enterprises corporation (livemode). Confirm it is the intended merchant before installing CallPilot's restricted key.
- Not production-validated: inbound assistant admission, human failover, concurrent calls, provider recording playback, Stripe payment+webhook scenarios.

## Completed in software
- Customer portal shows published plan and mandatory setup fee.
- Stripe test/live modes prepared; live mode explicitly gated by billing_enabled + live_approved + correct key + signed webhook + four fixed Price IDs. Existing sandbox Stripe customer can't silently become live.
- Subscription webhook supports paid setup, customer.subscription create/update/delete and invoice.payment_failed.
- Monthly usage ledger based on provider call records; period start/end from Stripe; 80/95/100% in-dashboard alerts deduplicated.
- Inbound admission only when a paid, setup-complete, linked business has provider routing configured and verified billing cycle.
- When exhausted: no new AI conversation; explicit human forwarding is optional and can incur PSTN charges.
- When server fails: opt-in Vapi call.ending fallback hook can forward to human or announce unavailable without starting AI.
- Four-minute limit on Vapi assistant: hard cap at 240s, wrap-up 215s.
- Owner may set verified human backup number; no automatic charges or purchases triggered by this app.
- Safe Vapi number association from unique call history, if Vapi confirms assistant relationship.

## Blocking requirements before charging customers
1. **Always-on infrastructure.** Upgrade the Render service to paid always-on hosting after owner approves recurring cost. Add health checks and alerting; Free sleep can exceed Vapi's 7.5-second assistant-request window.
2. **Correct merchant account.** Confirm intended Stripe account. Use a dedicated restricted key (not another application's raw secret, never paste into chat). Configure STRIPE_SECRET_KEY, STRIPE_MODE=live, STRIPE_WEBHOOK_SECRET, STRIPE_BILLING_ENABLED=true, STRIPE_LIVE_APPROVED=true, and fixed price IDs STRIPE_PRICE_STARTER/GROWTH/BUSINESS/SETUP in Render. Check product amounts and intervals.
3. **Sandbox-to-live reconciliation.** Existing sandbox billing customer must not be reused as a live customer. Reconcile/clear or migrate only after explicit confirmation.
4. **Real payment test.** Complete authorized live/sandbox flows as appropriate: first charge with setup, monthly renewal without setup, portal invoices, plan change, cancel, refund policy, failed payment, duplicate/reordered webhook, and signed signature test.
5. **Phone routing.** Verify unique Vapi provider ID and associated assistant in app; create Vapi Bearer credential matching server VAPI_WEBHOOK_TOKEN; configure VAPI_CREDENTIAL_ID in Render; activate only after signed webhook, active billing and test.
6. **Overflow tests.** Simulate exhausted allowance, same-time inbound calls, webhook outage, optional human transfer, carrier failure, busy/no answer, and limited carrier credits. A human transfer can be billed separately even with AI off.
7. **Service capacity.** Vapi concurrency is subscription-shared (usage-only plan base capacity should be verified in account). Ten numbers do not guarantee ten simultaneous calls. Monitor and upgrade at measured peaks.
8. **Security/privacy.** Make repo private, audit Supabase tenant boundaries, require recording consent/disclosures where applicable, add retention/deletion policy, secure recording URLs and export logs.
9. **Commercial/legal.** Publish terms of service, privacy policy, recorded-call disclosures, $100 setup fee refund and cancellation terms, monthly call limits and four-minute cap, support contact and response policy.
10. **Operations.** Run end-to-end browser, desktop/mobile, real phone, SMS/email verification and accessibility tests; establish uptime monitoring and customer support procedure.

## Market differentiation
- Smith.ai sells per call with onboarding and analytics.
- Goodcall sells by unique customer and includes a number, often with no minute charges.
- Upfirst sells monthly call allowances with transfers and instant summaries.
- CallPilot's advantage is approved business knowledge + semantic answers + call lead intelligence. Its disadvantage today is unfinished phone provisioning, hard 4-minute cap, and absence of verified always-on reliability.

## Customer-facing exhausted policy
- At 80%: gentle usage alert with balance and renewal period.
- At 95%: urgent alert, upgrade/renewal action.
- At 100%: stop accepting **new AI calls** after provider admission routing is activated; don't disconnect the caller already speaking. Existing call ends by four-minute timeout.
- If owner opted into forwarding: return a direct human transfer destination, no AI, carrier fees possible.
- Otherwise: provider unavailable announcement and disconnect; no unexpected AI overage.
- Upgrade or next billing cycle restores eligibility after webhook and usage ledger confirm.

## Actual next step for this account
Do not begin unrestricted paying customer onboarding. Recruit 2–3 supervised pilots, collect setup requirements, verify merchant account and approve always-on hosting, then test the full live workflow before enabling paid calling limits.
