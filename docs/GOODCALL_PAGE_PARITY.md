# Goodcall page parity

This is the page and interaction inventory. See [the separate feature checklist](GOODCALL_PARITY.md) for backend behavior, dependencies, and end-to-end evidence. A checked row requires a working CallPilot counterpart for the entire named surface, including navigation, forms, validation, loading, empty and error states. An unchecked row records the remaining work. **Observed** means opened in the browser. **Documented** means found in official material. **Pending** means the complete workflow or its entry point has yet to be inspected.

## Public site

| Done | Goodcall page or page family | Evidence | CallPilot destination and remaining work |
| --- | --- | --- | --- |
| [ ] | `/` homepage | Observed | `/` has a redesigned navigation, signup entry point, and interactive receptionist walkthrough. Live desktop walkthrough and scheduling link verified. Added selectable lead, booking, CRM, and commerce workflows, plus agent identity, call review, forwarding, and resource sections. Mobile, proof, and comparison coverage remain. |
| [ ] | `/pricing` | Observed | `/pricing` shows current CallPilot plans. Includes a comparison table sourced from actual plan configuration and a billing FAQ. Checkout verification remains. |
| [ ] | `/how-it-works` | Observed | `/how-it-works` covers setup, test, and connection. Expand walkthrough as setup and practice features ship. |
| [ ] | `/faq` | Observed | `/faq` has expandable original answers. Complete topic coverage. |
| [ ] | `/answering-services` | Observed | `/answering-services` links to Launch Studio. Expand comparison and industry content. |
| [ ] | `/appointment-scheduling` | Observed | `/appointment-scheduling` explains booking and links to Appointments. Carrier and calendar tests are tracked separately. |
| [ ] | Lead capture, catalog and order, call review, and phone setup | Homepage and product documentation observed | Dedicated CallPilot pages link to the corresponding workspace tools. Expand industry-specific journeys. |
| [ ] | `/business-productivity-ai` and individual integration pages | Index observed, child enumeration pending | Directory and detail pages cover Google Calendar, HighLevel, Shopify and Vapi. Goodcall’s broader catalog remains. |
| [ ] | `/business-phone-number` area-code lookup | Observed | CallPilot offers a public area-code preference form that carries the area code into Channels for eligible owners. Live carrier inventory browsing and country coverage remain open. |
| [ ] | `/local-phone-numbers` | Observed | `/local-phone-numbers` explains reservation, number connection, and forwarding. Live area-code inventory and self-service reservation verification remain. |
| [ ] | `/enterprise` | Observed | Enterprise details and lead entry point missing. |
| [ ] | `/case-study-goodcall-ai-phone-assistant` and case-study details | Index observed, details pending | CallPilot case studies and proof pages missing. |
| [ ] | `/about` | Observed | `/about` describes CallPilot’s supported product and privacy workflow. |
| [ ] | `/news` | Observed | `/news` lists actual CallPilot product updates. |
| [ ] | `/blog` and article details | Index observed, child enumeration pending | Searchable `/resources` and five original setup guides now cover core journeys. Editorial and industry coverage remains. |
| [ ] | `/voice-ai` and article details | Index observed, child enumeration pending | Educational index and article template missing. |
| [ ] | Help Center index, collection and article pages | Index observed, collections and articles partially documented | `/support` offers public guide search, help categories, FAQ and workspace help links. Additional support collections remain. |
| [ ] | Industry, geographic, comparison and campaign landing-page families | Discovery pending | Inventory linked routes and model reusable original detail pages. |
| [ ] | Privacy, terms, trust and support links | Footer and official help observed | `/legal` exists. Inventory individual public pages and their navigation. |

## Sign-in and onboarding

| Done | Goodcall page or surface | Evidence | CallPilot destination and remaining work |
| --- | --- | --- | --- |
| [ ] | Sign-in, email/Google options, account recovery and errors | Login observed, recovery end-to-end pending | `/app` supports email and Google. Signed-in dashboard verified. Verify recovery and persistence across reloads. |
| [ ] | Onboarding business lookup | Observed | Optional Google Maps search in guided setup displays attributed listings. Live name search returned attributed listings. Exact phone matching and confirmed selection remain open. Workspace information is entered separately. |
| [ ] | Website input and scan progress | Input observed, authorized user supplied a site | Sign-up (`/app?mode=signup`) is now a business, website, account wizard whose answers become the workspace and an unapproved website draft after first sign-in (implemented but untested live). Details in GOODCALL_ONBOARDING_FLOW.md. Live QA created a workspace, imported a public page, and opened the unapproved draft in Knowledge. Live QA also refreshed the website step in the same tab, resumed the same workspace, and skipped to Launch Studio. Multi-page discovery and structured metadata review remain. |
| [ ] | Manual business details | Observed | Guided setup collects name and industry and offers a path to Launch Studio. Timezone and structured details remain. |
| [ ] | Initial build progress and first-practice prompt | Observed | Launch Studio has a checklist. Generated agent, progress, and voice practice missing. |
| [ ] | Welcome, finish setup and testing navigation | Observed | Launch Studio and text test partially cover this. Test with no number and publishing remain. |

## Authenticated app

`/app#workers` supports creating, editing, reordering and disabling named call playbooks. Channels can enable separate runtime specialists and deterministic inbound rules. The operator supports a spoken menu, and runtime specialists can save structured intake to the call. Multi-step branching and provider verification remain open.

The `/app#orders` review page supports staff approval, Shopify checkout creation and recovery, a copyable checkout link, live payment and fulfillment status, and shipment review and tracking entry for eligible paid Shopify orders, plus a Shopify shortcut for other shipping options. Customer confirmation and live checkout and fulfillment testing remain open.

| Done | Goodcall page, tab, or dialog | Evidence | CallPilot destination and remaining work |
| --- | --- | --- | --- |
| [ ] | `/agent/dashboard` and persistent Gabi composer | Observed | Sidebar now follows Goodcall: Assistant, Analytics, Sessions, Channels; Identity, Workers, Connections; a collapsible More group for Knowledge, Appointments, Order requests, Setup, Test assistant and Settings; account menu and Support at the bottom. The assistant header shows the workspace, live version and publish state. CallPilot opens at `/app#builder` with private persistent conversation, new-conversation control, review panel, and a desktop assistant dock. Voice input, feedback, and worker generation remain. |
| [ ] | `/agent/performance`, date presets/custom range and empty results | Observed | `/app#analytics` shows date filters, call totals, a daily volume chart, and lead outcomes. Owners and admins can add or remove saved metrics by lead outcome, call direction, duration, and open follow-up. Worker attribution and runtime verification remain. |
| [ ] | In-call branching editor | Goodcall instructions and branch controls observed | CallPilot Workers includes a spoken menu editor with named and numbered branches, readiness validation, and sync. Multi-step graphs and live menu verification remain open. |
| [ ] | `/agent/calls`, search and session list | Observed | `/app#calls` search and date controls verified. Goodcall populated session-detail screens remain uninspected in the empty reference account. |
| [ ] | Session detail, transcript, recording, and tags | Documented, populated Goodcall detail pending | CallPilot detail panel exists. Verify playback, edits and error states. |
| [ ] | `/agent/channels`, shareable demo and link tracking | Observed | Channels shows Goodcall-style Phone, Shareable demo and Browser practice cards (Share jumps to demo link creation). The link-shortening toggle is not built because CallPilot does not text links. `/app#channels` shows actual linked phone and browser practice availability. Owners and admins can create, copy, open and revoke expiring text-demo links with per-link question counts. Optional one-minute voice demos require operator enablement, workspace allowlisting, and explicit link opt-in. Visit-level tracking and live voice verification remain. |
| [ ] | Public product voice demo | Public entry point | `/voice-demo` is linked from the homepage and public navigation. It opens the specifically published workspace voice demo when enabled and provides the homepage call example otherwise. Live public access has not been enabled or verified. |
| [ ] | Channels phone connection and number acquisition dialogs | Entry point observed, provisioning flow blocked | CallPilot Channels has owner-only U.S. area-code reservation and pending-number recovery. Usage & phone has a separate activation form. Live number reservation and activation are deferred. Opening Goodcall Connect remains blocked by possible provisioning or charges. |
| [ ] | `/agent/identity` overview | Observed | `/app#voice` opens on an overview with a persona card, Voice, Keywords and Language cards and Address, Hours and Website cards; each opens its configure tab with a back link. Observed layout recorded in GOODCALL_ONBOARDING_FLOW.md; avatar editing remains. |
| [ ] | Identity configuration, Persona tab | Observed | Identity has an edit/view mode, greeting, name, stock voice selection, and a link to provider samples. Avatar and in-page samples remain. |
| [ ] | Identity configuration, Speech tab | Observed | Identity saves bilingual or fixed English/Spanish settings, recognition terms, and pronunciation hints. Sync updates managed provider configuration. Live speech verification remains. |
| [ ] | Identity configuration, Business Profile tab | Observed | Identity saves business name, industry, address, and website. Google Business Profile linking and sync remain. |
| [ ] | Identity configuration, Open Hours tab | Observed | Identity and Business settings share the same timezone, weekly booking hours and exception editor. Spoken hours and after-hours routing remain. |
| [ ] | Identity read-only/edit state and History drawer | Observed | Identity includes edit/view mode and links to previous saved greeting and instructions. The builder adds a Publish action, live/unpublished status, numbered published versions and rollback. Versioning for avatar and voice fields and a dedicated history drawer remain. |
| [ ] | `/agent/workers`, worker list and Add worker | List and Create Worker dialog observed, including Free Prompt and Structured Form | Workers opens on a tree of the operator and its workers (matching Goodcall's list), with practice-call and add-worker buttons, plus Playbooks, Intake forms and Call menu views and a Knowledge shortcut. Per-worker tool badges, dedicated worker detail routes and history remain. |
| [ ] | Operator detail, Greeting tab | Observed | `/app#voice` supports the greeting and an optional closed-hours greeting chosen at call admission (untested live). After-hours routing rules remain. |
| [ ] | Operator detail, Directory tab | Observed | Channels stores human transfer destinations with routing descriptions, enable controls and managed assistant sync. Escalation defaults, failure behavior and live verification remain. |
| [ ] | Worker detail, specialist and tool configuration | MetroPrint Lead Capture fields and post-form tools inspected | CallPilot Workers supports question type, required or optional answers, post-form return or call end, and a saved-answer viewer. Email and SMS chains and specialist history remain open. |
| [ ] | Workers, Knowledge tab and source list | Observed | `/app#knowledge` has source attribution, editable drafts, approval, and search testing. |
| [ ] | Knowledge, Add document dialog, Upload PDF and Add Link tabs | Dialog, PDF tab and link form observed | Website link and PDF import verified in the authenticated sandbox. PDF extraction creates a reviewable draft. Scanned-document OCR remains open. |
| [ ] | Knowledge source detail and lifecycle controls | Source detail, View content, Edit and History observed | CallPilot edits and approval available. Compare source detail and deletion behavior. |
| [ ] | `/agent/connections`, Integrations tab | Observed | Connections now has Integrations and Custom APIs tabs with OAuth, Voice and phone, API key and MCP server groups; Google Sheets lead export is built (untested live, needs its migration); Zapier appears as Coming soon, and Custom APIs is an honest empty state. `/app#connections` reports live Calendar, Vapi, and business phone status and links to management. The Connections directory can filter Calendar, Voice & phone, or CRM & commerce. Live sandbox status and Calendar navigation were previously verified. The Appointments page now supports disconnect, booking creation, filtering, rescheduling, and calendar-backed cancellation with pending-operation recovery. HighLevel and Shopify have connection, disconnect and lookup controls for contacts and inventory respectively. Remaining providers and live checks are open. |
| [ ] | Customer booking link and public appointment form | Documented scheduling link workflow | Appointments can issue a single-use expiring link. The customer sees the service, checks calendar availability, and books without signing in. Live booking verification and public reschedule/cancel remain open. |
| [ ] | Connections, Custom APIs tab and Add Custom API dialog | Observed | Build scoped endpoint definitions and validations. |
| [ ] | Connections, custom MCP and tool-request forms | Custom MCP name, URL, and optional bearer token fields observed. Tool-request submission pending. | No equivalent yet. |
| [ ] | `/agent/settings`, Profile and account security | Observed | Settings uses Goodcall-style Profile, Team and Billing underline tabs, first and last name, organization and a guarded Delete account section (untested live). Recovery by email still works through the sign-in screen. |
| [ ] | Settings, Team and Invite team member dialog | Observed, no invitation sent | Workspace team list, link creation, role selection and revocation are implemented. Email delivery, optional message, role editing and recipient acceptance verification remain open. |
| [ ] | Settings, Billing summary, payment method and invoices | Observed | `/app#billing` has plan entry points and Stripe portal. Verify setup, payments and invoice history in sandbox. |
| [ ] | Account menu, agent switcher, new agent and sign-out | Menu observed, creation workflow pending | Workspace switcher and sign-out exist. Multi-agent management missing. |
| [ ] | Support control and contextual help | Entry point observed | `/app#help` exists. Compare support dialogs and escalation. |
| [ ] | Browser practice, microphone prompt, typing and return to setup | Entry points observed, live voice exchange pending | `/app#simulator` verified for text retrieval and repeatable saved answer checks. Voice practice and an in-session transcript are implemented behind the provider-use flag. Spoken verification, sharing and recorded evaluations remain open. |
| [ ] | Mobile navigation, menus, empty/loading/error states and accessibility | Some desktop states observed, mobile inventory pending | Validate every implemented surface at mobile breakpoints and with keyboard navigation. |

## Coverage limits

This inventory covers the routes and surfaces discovered so far. Linked article and landing-page children, empty-account-dependent states, paid/provider workflows, and settings that require external changes are still open. Page groups explicitly marked pending must be expanded into individual entries before a claim of exhaustive page coverage.
