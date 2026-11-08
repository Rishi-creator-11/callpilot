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

## Quick setup (guided agent builder, inspired by Retell AI's setup flow)
- Requested flow: import the website, tick what the agent should do, let the product build the agent, then test it live. No long prompts from the owner.
- Implemented: `/app#quicksetup` (Assistant page button, More menu and the end of sign-up lead here). Step 1 imports one website page and shows what was found with a single "this looks right" approval. Step 2 is a checklist (answer questions, take messages and leads, collect quote details, take orders, book appointments, connect callers to a person) plus one optional line. Step 3 builds the greeting and instructions on the server from the business name and the goals, switches on only abilities whose prerequisites exist, creates the managed assistant if missing, syncs it, and lists what is still needed (connect Calendar, approve a product, add a transfer number). Step 4 tests by text now and links to voice practice and Channels.
- Status: implemented but untested live. Retell's own dashboard was not inspected (it needs the owner's login), so this follows the owner's description. Voice testing depends on the voice practice flag and daily limits.

## Quick setup v2 (informed by Retell's onboarding assistant)
- Observed in Retell (read-only): the assistant reads the website, asks one question about what the agent should mainly do, builds the agent, then explains in plain English how a call will go, what else is set up, and two things to know. It ends with a test call and a Getting started checklist.
- CallPilot now: three ways to share the business (website, PDF, or typed or spoken text), then a review of what we understood with an editable overview and prices we found. Prices are only kept when their exact wording is in the owner's own text, and are published only after the owner ticks them and confirms they are current. Then the goals checklist, an automatic build, a chat test, and a finish button that opens the dashboard. First-time owners are sent here right after signing in; Skip for now goes to the workspace.
- Website import now skips menus, buttons and footers and also reads a few useful pages on the same site (about, services, pricing, contact), up to 4 extra.
- Plain U.S. phone numbers are accepted for call transfer.
- Not done: a Getting started progress checklist on the dashboard, a call-by-call explanation of how the agent will behave, and image-only PDFs.

## Products vs services (business-aware setup)
- Setup now starts with "What does your business do?": we sell products, or we provide services (clinic, salon or wellness, home or trade services, professional or digital services, or other). The answer sets sensible default abilities and shapes the agent's instructions and what we pull out of the website.
- Services: the review step lists services with how long each takes, plus policies such as cancellation, insurance and new-customer steps, hours and contact details, so the agent can explain a service and move toward a booking. Without a connected calendar the agent saves an appointment request instead of claiming to book.
- Clinics: instructions follow common practice from vendor guidance (booking, rescheduling and cancelling as the core jobs; new or returning patient; collect only name, number, visit type and time; no medical advice or urgency judgments; emergencies go to 911). CallPilot is not HIPAA certified and has no signed agreements with its providers, so clinics are told not to use it for patient health information.
- Talk to your agent: after setup, "Call your agent" starts a browser call with the real agent and its real tools (bookings and requests are real, so use made-up details); "Practice call" answers from approved knowledge only. Both need VOICE_PREVIEW_ENABLED=true on the server, and the workspace allowed if VOICE_PREVIEW_WORKSPACES is set.
- Sources: https://www.cliniqapps.com/ai-receptionist, https://www.primepro.ai/clinics, https://vortalabsai.com/blog/hipaa-compliant-ai-receptionist (vendor pages; treat as practical checklists, not legal advice).

## Simplified navigation (owner request)
- Sidebar has three places: Agent, Analytics, Channels. Account, Team and billing live in the account menu; Support stays at the bottom.
- Agent tabs: Setup, Identity, Workers, Knowledge, Connections, plus a Test agent button. Test agent opens a live circle: tap it to start a voice call with the real agent (typing also works). Voice needs VOICE_PREVIEW_ENABLED=true.
- Analytics tabs: Overview, Calls, Appointments, Orders. Overview counts appointments booked for service businesses and orders received for product businesses (both for older workspaces with no business type). Sessions and Analytics are now one area.
- Channels: Phone and Shareable demo only. Share my agent creates or reuses one link to the finished agent and copies it. Call screening and routing, browser practice and the link label form are removed from the screen.
- The old Assistant chat, Setup, Test assistant and Settings pages still exist but are no longer in the sidebar.

## Whole-site reading (owner priority)
- Importing a website now reads the whole site: the sitemap and key pages in parallel (up to 90 pages in about 8 seconds on the real MetroPrint site), through the same SSRF-safe downloader. It reads structured data many sites embed (JSON-LD and Next.js product records with every size, paper and quantity option and a price matrix), so product names, options and prices are exact, never guessed.
- The review step shows the business description, categories, contact details, every product with its advertised "From" price and options, and the policy pages. The owner can untick anything. Prices are only used if the owner confirms them.
- Saving publishes one approved answer per product and per policy page (embedded in parallel), tied to the overview document so re-running replaces them. Amounts are stripped from answers unless prices are confirmed.
- The AI summary step uses the Vapi chat API. If that call fails (for example HTTP 401), the summary falls back to the site's own description and extracted contact details; products and prices do not depend on the AI step.
- Verified on https://www.metroprintmkt.com/: 85 pages read, 73 products matching the site's own count, Flyers advertised from $42.49, refund and reprint policies, phone and email.

## Best practices applied to agent knowledge (research notes)
Sources are vendor docs and guides, so treat them as practical guidance, not independent benchmarks:
- Keep behavior in the prompt and facts in the knowledge base; write for the ear, one question with one clear answer; use structured question-and-answer content, not raw page dumps; tell the agent to answer only from retrieved facts and to fall back politely when nothing matches ([Retell: knowledge base](https://docs.retellai.com/build/knowledge-base), [Retell: structuring a voice knowledge base](https://www.retellai.com/blog/how-to-structure-knowledge-base-voice-ai-prevent-hallucinations), [Vapi prompting guide](https://docs.vapi.ai/prompting-guide)).
- Test with a list of real caller questions: direct, vague, out-of-scope and trick requests, and knowledge questions versus action requests; review real transcripts after launch and update content when prices or policies change ([Newo: what to include](https://newo.ai/insights/knowledge-base-for-voice-agents-what-to-include-faq-pricing-policies), [OptimizeSmart](https://optimizesmart.com/blog/voice-ai-knowledge-base-creation-best-practices/)).
What CallPilot now does: reads the whole site; stores one approved answer per product (sizes, options, price when confirmed), per category, per policy and per FAQ; keeps price units exactly as the site states them ($75/hr, Packages from $499); and runs an automatic knowledge test of caller-style questions after saving, with a score and the list of misses. Retrieval only answers when a stored question closely matches (similarity 0.82) and returns one answer, so the test is how we find phrasing that misses.
