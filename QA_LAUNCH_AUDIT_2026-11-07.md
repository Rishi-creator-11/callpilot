# CallPilot launch verification — 2026-11-07

## Approved commercial offer
- Starter: $49/month, 100 minutes.
- Growth: $99/month, 300 minutes.
- Business: $179/month, 600 minutes.
- Mandatory **$25 one-time setup fee** per new business.
- First payment before tax: Starter $74; Growth $124; Business $204.
- Actual usage measured in minutes, not a daily call quota. Max 240 seconds per Vapi assistant call.

## Changes verified this session
- New **live Stripe** $25 one-time Price created for the existing CallPilot professional setup product.
- The product's default price was updated to $25, then its old $100 Price was deactivated. Prior payments were not modified.
- Render environment `STRIPE_PRICE_SETUP` updated to the new $25 Price ID, preserving all other environment variables.
- Backend `SETUP_FEE_CENTS` updated to 2500; customer billing page updated and guest login page remains free of pricing.
- Stripe sandbox Price IDs now have a separate `STRIPE_TEST_PRICE_*` namespace, so staged live Price IDs cannot break sandbox Checkout.
- The GitHub CI suite passed **90 unit tests** and frontend JavaScript syntax/compilation checks on commit 628c266.
- A GitHub Actions **live read-only smoke test** passed: Render app HTML includes $25 pricing, health endpoint returned HTTP 200, public authentication configuration returned HTTP 200, and anonymous requests to account/billing/usage APIs returned HTTP 401.
- Database uniqueness constraint added and verified on linked Vapi assistant IDs and phone IDs, preventing two customer workspaces from sharing provider identities.
- RLS security advisory reviewed: 15 server-only tables intentionally deny clients; direct `anon`/`authenticated` table grants found none.
- No new phone numbers, customer accounts, payments, or live routing cutovers were performed.

## Observed or unresolved blockers — do not mark live-ready
1. **Render Free**: service remains Free. Move to paid always-on hosting before business phone calls depend on it.
2. **Live payments off**: Stripe catalog is configured but a complete signed webhook + successful subscription checkout + $25 setup collection + renewal + cancellation + failed-payment test has NOT been performed. Do not enable live charging without explicit operator approval.
3. **Inbound minute enforcement not activated**: monthly 100/300/600-minute admission control and four-minute cap must be tested with a real phone number; no overage charges should be enabled.
4. **Human fallback not live-tested**: verify transfer on exhausted quota, busy/unavailable destination, Vapi timeout, and unreachable Render server. Human forwarding may incur carrier charges.
5. **Call recordings and transcripts not fully end-to-end verified** in authenticated production browser session.
6. **Browser signup, verification, password reset, Google Calendar OAuth, and mobile accessibility** have not received a complete live human/browser test.
7. **GitHub repository public**. Make private if appropriate before public launch; check vendor deploy integration permissions.
8. **Supabase leaked-password protection disabled**: enable if account/plan permits. See Supabase authentication password settings.
9. **Policies and compliance**: verify terms, privacy, recording consent, retention/deletion, setup fee refunds and cancellation language, incident/support process.
10. **Operational reliability**: add uptime/error alerting, concurrent-call capacity tests, database backup/restore and billing reconciliation.

## Go/no-go
**NO-GO for unsupervised paid production launch.** Allowed: demos and supervised non-billing pilots with owner permission.

## Safe next sequence
- Set up always-on Render with explicit cost approval.
- Test Stripe sandbox end-to-end and perform authorized live payment verification.
- Make and review test calls through usage admission, including exhausted quota and human fallback.
- Verify browser lifecycle, security, recording retention and terms.
- Approve production activation of billing and phone admission only after each gate passes.
