# CallPilot product interface standards

Applied design guidance for CallPilot's business-facing AI receptionist workspace.

## Reviewed source skills / references
- Anthropic Frontend Design: https://github.com/anthropics/skills/tree/main/skills/frontend-design
- Vercel Web Interface Guidelines: https://vercel.com/design/guidelines
- UI/UX Pro Max (reference for design systems and web/mobile review): https://github.com/nextlevelbuilder/ui-ux-pro-max-skill
- WCAG 2.2: https://www.w3.org/TR/WCAG22/
- Nielsen Norman Group mobile usability research: https://www.nngroup.com/reports/mobile-website-and-application-usability/

## Product goal
Audience: busy small-business owners and receptionists managing inbound calls, prospects and business knowledge.
Primary job: review important customer calls and choose the next follow-up in seconds.

## Design direction
An operations cockpit, not an analytics poster: navy navigation for context, clean off-white content surfaces, crisp borders, functional lavender for primary actions, and status colors restricted to real operational states.

## Rules to maintain
1. One task-focused headline per screen; use human language rather than provider terms.
2. Primary action unambiguous; secondary actions grouped. Never show a button that performs no action.
3. Consistent sidebar, profile docked at bottom, mobile drawer with real menu toggle and dismiss.
4. Entire UI keyboard-operable; visible focus rings, Escape dismisses menus, focus moves sensibly.
5. Mobile hit targets >=44px and input font sizes >=16px; layouts usable at 320px.
6. Handle loading, empty, error, and success states; errors give the next recoverable action.
7. Keep state/selection in the URL where safe; never put sensitive data in URLs.
8. Prefer accessible native controls. Recordings have transcript alternatives when available.
9. Lead labels are *suggested* unless human-reviewed. Never equate hot lead with a verified buyer.
10. No data mocks in the deployed business experience; reflect real Supabase/Vapi state.
11. Do not alter backend authorization or expose privileged credentials.
12. Honor reduced motion, browser zoom and 200%-400% text scaling.
13. Buttons must describe their action (e.g. Sync calls) and prevent duplicate submissions.
14. Run JS/HTML checks after changes; test guest/auth, desktop/mobile, call detail, signup, and billing.
15. Document unavailable integrations honestly. Never imply an untested flow is complete.

## Release checklist
- [ ] Signup / password reset / profile workflow through browser
- [ ] Sidebar navigation mouse, keyboard and mobile screen reader
- [ ] Call filter, detail, manual review, recording, CSV export
- [ ] Business/tenant isolation and permissions
- [ ] Billing portal and test checkout
- [ ] 320, 375, 768, 1024, 1440 responsive visual review
- [ ] Color contrast and accessible names
- [ ] No secrets in browser code
