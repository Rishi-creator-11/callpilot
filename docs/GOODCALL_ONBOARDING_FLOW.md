# Goodcall sign-up and onboarding flow (observed)

Observed November 7, 2026 in the built-in browser while the owner walked through sign-up using a friend's business as a sample. Sample business details are intentionally not recorded. CallPilot's build must stay generic for any business, website and feature set. Wording and assets below are described, not copied.

Progress is shown as a six-dot stepper with a small mascot above the heading and a back arrow. Layout is a single centered column on a plain background with one primary button. Each step also offers "Already have an account? Sign in".

| Step | Route | What the user sees | CallPilot today |
| --- | --- | --- | --- |
| 1 | `/agent/onboard` | Enter business phone number with a country selector. Primary action finds the business on Google Maps. Secondary action for businesses not on Google Maps. | Account first, then optional Maps search inside guided setup. |
| 2 | `/agent/onboard` | Website step: scan the site to build an agent that knows the business. Secondary action for no website. | Website import exists but runs after the workspace is created. |
| 3 | `/agent/onboard/scanning` | Welcome screen personalized with the business name and a rotating explainer while the scan runs, with a Stop control. | No progress screen. |
| 4 | `/agent/onboard/verify-phone` | Owner's mobile number is texted a 4-digit code. Copy explains it is used for lead and message notifications. Four single-digit boxes, Verify, resend countdown (about 30 seconds), change number. | No verified owner notification number. |
| 5 | `/agent/onboard/signup` | Create account last, framed as saving the agent. Fields: organization name, first name, last name, work email, password. Live password rules (8+ characters, uppercase, lowercase, number). Product-updates checkbox. Create account, Sign up with Google, terms and privacy consent. Existing email shows an inline error with a Log in instead link. | Sign-in screen with email and Google, workspace named after sign-up. |
| 6 | After sign-up | Not yet observed. | |

## Implementation backlog derived from this flow
- Pre-account onboarding: phone or name lookup, website scan with progress, then account creation that keeps the drafted agent. Needs a short-lived anonymous draft keyed to the browser session with expiry and rate limits.
- Verified owner notification number (SMS code, resend cooldown, change number). Requires a consent-aware SMS provider; defer sending until authorized.
- Account form fields: organization name, first and last name, live password rules, marketing opt-in kept separate from terms consent, duplicate-email handling.
- Step 6 onward (build progress, first practice prompt, dashboard) to be recorded once observed.

## CallPilot status (2026-11-07)
- Implemented but untested live: `/app?mode=signup` is a three-step get-started wizard (business, website, account) with progress dots and back links. Account step has first and last name, work email, live password rules, product-updates checkbox, Google sign-up, terms link and a duplicate-email message with a Log in instead link.
- Business and website answers are saved as a non-sensitive draft in the browser for seven days. After the first sign-in with no workspace, the draft creates the workspace and imports the website as an unapproved draft in Knowledge.
- Not built: business lookup by phone before the account exists (the Maps search needs a signed-in user to protect paid API usage), the scanning welcome screen, and the 4-digit SMS verification (needs an approved SMS provider and consent text).

## Dashboard and navigation (observed, mobile-width pane)
- Dashboard `/agent/dashboard`: header with business name, new-conversation button and a status line (version live, all changes published). Chat with the builder assistant, composer with voice-mode button, thumbs up/down feedback per reply, bottom switcher between the builder assistant and a test of the agent, mute toggle.
- Sidebar: logo and close; builder assistant, Analytics, Sessions, Channels; divider; Identity, Workers, Connections; footer with account switcher and Support. Not recorded: feedback persistence and the voice-mode flow.

## Channels and Connections (observed, mobile-width pane)
- Channels `/agent/channels`: Phone card (Connect, plus Get a Number in the header), Shareable demo card (Share copies a no-sign-in link), and a Shorten and track links in texts toggle with explanatory copy.
- Connections `/agent/connections`: tabs Integrations and Custom APIs (count). Groups OAuth (Google Sheets, Google Calendar), API key (GoHighLevel), MCP server (Zapier actions) with Connect buttons, an empty MCP state and an Add custom MCP server action.
