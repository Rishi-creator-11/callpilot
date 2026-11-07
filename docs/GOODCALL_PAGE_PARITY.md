# Goodcall page parity

This is the page and interaction inventory. See [the separate feature checklist](GOODCALL_PARITY.md) for backend behavior, dependencies, and end-to-end evidence. A checked row requires a working CallPilot counterpart for the entire named surface, including navigation, forms, validation, loading, empty and error states. An unchecked row records the remaining work. **Observed** means opened in the browser. **Documented** means found in official material. **Pending** means the complete workflow or its entry point has yet to be inspected.

## Public site

| Done | Goodcall page or page family | Evidence | CallPilot destination and remaining work |
| --- | --- | --- | --- |
| [ ] | `/` homepage | Observed | `/` has a redesigned navigation, signup entry point, and interactive receptionist walkthrough. Live desktop walkthrough and scheduling link verified. Added selectable lead, booking, CRM, and commerce workflows, plus agent identity, call review, forwarding, and resource sections. Mobile, proof, and comparison coverage remain. |
| [ ] | `/pricing` | Observed | `/pricing` shows current CallPilot plans. Add customer-facing plan comparison and verify checkout separately. |
| [ ] | `/how-it-works` | Observed | `/how-it-works` covers setup, test, and connection. Expand walkthrough as setup and practice features ship. |
| [ ] | `/faq` | Observed | `/faq` has expandable original answers. Complete topic coverage. |
| [ ] | `/answering-services` | Observed | `/answering-services` links to Launch Studio. Expand comparison and industry content. |
| [ ] | `/appointment-scheduling` | Observed | `/appointment-scheduling` explains booking and links to Appointments. Carrier and calendar tests are tracked separately. |
| [ ] | Lead capture, catalog and order, call review, and phone setup | Homepage and product documentation observed | Dedicated CallPilot pages link to the corresponding workspace tools. Expand industry-specific journeys. |
| [ ] | `/business-productivity-ai` and individual integration pages | Index observed, child enumeration pending | Build integration directory and provider detail pages. |
| [ ] | `/business-phone-number` area-code lookup | Observed | Standalone lookup and area-code pages missing. |
| [ ] | `/local-phone-numbers` | Observed | `/local-phone-numbers` explains number connection and forwarding. Live area-code inventory and self-service provisioning remain. |
| [ ] | `/enterprise` | Observed | Enterprise details and lead entry point missing. |
| [ ] | `/case-study-goodcall-ai-phone-assistant` and case-study details | Index observed, details pending | CallPilot case studies and proof pages missing. |
| [ ] | `/about` | Observed | Company page missing. |
| [ ] | `/news` | Observed | News page missing. |
| [ ] | `/blog` and article details | Index observed, child enumeration pending | Searchable `/resources` and five original setup guides now cover core journeys. Editorial and industry coverage remains. |
| [ ] | `/voice-ai` and article details | Index observed, child enumeration pending | Educational index and article template missing. |
| [ ] | Help Center index, collection and article pages | Index observed, collections and articles partially documented | In-app Help & support exists. Public support search and collections missing. |
| [ ] | Industry, geographic, comparison and campaign landing-page families | Discovery pending | Inventory linked routes and model reusable original detail pages. |
| [ ] | Privacy, terms, trust and support links | Footer and official help observed | `/legal` exists. Inventory individual public pages and their navigation. |

## Sign-in and onboarding

| Done | Goodcall page or surface | Evidence | CallPilot destination and remaining work |
| --- | --- | --- | --- |
| [ ] | Sign-in, email/Google options, account recovery and errors | Login observed, recovery end-to-end pending | `/app` supports email and Google. Signed-in dashboard verified. Verify recovery and persistence across reloads. |
| [ ] | Onboarding business lookup | Observed | Optional Google Maps search in guided setup displays attributed listings. Live name search returned attributed listings. Exact phone matching and confirmed selection remain open. Workspace information is entered separately. |
| [ ] | Website input and scan progress | Input observed, authorized user supplied a site | Live QA created a workspace, imported a public page, and opened the unapproved draft in Knowledge. Live QA also refreshed the website step in the same tab, resumed the same workspace, and skipped to Launch Studio. Multi-page discovery and structured metadata review remain. |
| [ ] | Manual business details | Observed | Guided setup collects name and industry and offers a path to Launch Studio. Timezone and structured details remain. |
| [ ] | Initial build progress and first-practice prompt | Observed | Launch Studio has a checklist. Generated agent, progress, and voice practice missing. |
| [ ] | Welcome, finish setup and testing navigation | Observed | Launch Studio and text test partially cover this. Test with no number and publishing remain. |

## Authenticated app

`/app#workers` supports creating, editing, reordering and disabling named call playbooks. Separate runtime workers and deterministic conditional actions remain open.

Order requests have a dedicated CallPilot review page at `/app#orders`. Provider order placement, fulfillment, and customer confirmation remain open.

| Done | Goodcall page, tab, or dialog | Evidence | CallPilot destination and remaining work |
| --- | --- | --- | --- |
| [ ] | `/agent/dashboard` and persistent Gabi composer | Observed | CallPilot opens at `/app#builder` with private persistent conversation, new-conversation control, review panel, and a desktop assistant dock. Voice input, feedback, and worker generation remain. |
| [ ] | `/agent/performance`, date presets/custom range and empty results | Observed | `/app#analytics` shows date filters, call totals, a daily volume chart, and lead outcomes. Configurable metrics, worker attribution, and runtime verification remain. |
| [ ] | `/agent/calls`, search and session list | Observed | `/app#calls` search and date controls verified. Goodcall populated session-detail screens remain uninspected in the empty reference account. |
| [ ] | Session detail, transcript, recording, and tags | Documented, populated Goodcall detail pending | CallPilot detail panel exists. Verify playback, edits and error states. |
| [ ] | `/agent/channels`, shareable demo and link tracking | Observed | `/app#channels` shows actual linked phone and browser practice availability. Shareable demo and link tracking remain. |
| [ ] | Channels phone connection and number acquisition dialogs | Entry point observed, provisioning flow blocked | CallPilot phone configuration exists. Automatic review blocked opening Goodcall Connect because it may initiate provisioning or a paid setup. Inspect only after a non-committing path is confirmed or the user opens it directly. |
| [ ] | `/agent/identity` overview | Observed | `/app#voice` partially maps to identity. Overview cards and navigation missing. |
| [ ] | Identity configuration, Persona tab | Observed | Agent and voice configuration partial. Stock voices, samples, and avatar editing need a counterpart. |
| [ ] | Identity configuration, Speech tab | Observed | Language, keyword, pronunciation and preview controls missing. |
| [ ] | Identity configuration, Business Profile tab | Observed | `/app#workspace` has basic business details. Profile sync and expanded fields missing. |
| [ ] | Identity configuration, Open Hours tab | Observed | `/app#workspace` has timezone editing, split appointment hours, and date overrides. Copy-day controls, spoken hours and after-hours routing remain open. |
| [ ] | Identity read-only/edit state and History drawer | Observed | Version history and approval workflow missing. |
| [ ] | `/agent/workers`, worker list and Add worker | List and Create Worker dialog observed, including Free Prompt and Structured Form | Build worker CRUD, validations, and assignment. |
| [ ] | Operator detail, Greeting tab | Observed | `/app#voice` supports greeting. Add open/closed greeting and routing rules. |
| [ ] | Operator detail, Directory tab | Observed | Build contact directory, escalation defaults and fallback routing. |
| [ ] | Worker detail, specialist and tool configuration | Some workers observed, detail forms pending | Build worker-specific panels and supported tools. |
| [ ] | Workers, Knowledge tab and source list | Observed | `/app#knowledge` has source attribution, editable drafts, approval, and search testing. |
| [ ] | Knowledge, Add document dialog, Upload PDF and Add Link tabs | Dialog, PDF tab and link form observed | Website link and PDF import verified in the authenticated sandbox. PDF extraction creates a reviewable draft. Scanned-document OCR remains open. |
| [ ] | Knowledge source detail and lifecycle controls | Source detail, View content, Edit and History observed | CallPilot edits and approval available. Compare source detail and deletion behavior. |
| [ ] | `/agent/connections`, Integrations tab | Observed | `/app#connections` reports live Calendar, Vapi, and business phone status and links to management. Live sandbox status and Calendar navigation verified. The Appointments page now supports disconnect, booking creation, filtering, and calendar-backed cancellation. HighLevel and Shopify have connection, disconnect and lookup controls for contacts and inventory respectively. Remaining providers and live checks are open. |
| [ ] | Connections, Custom APIs tab and Add Custom API dialog | Observed | Build scoped endpoint definitions and validations. |
| [ ] | Connections, custom MCP and tool-request forms | Custom MCP name, URL, and optional bearer token fields observed. Tool-request submission pending. | No equivalent yet. |
| [ ] | `/agent/settings`, Profile and account security | Observed | `/app#account` partially covers account settings. Verify recovery and deletion lifecycle. |
| [ ] | Settings, Team and Invite team member dialog | Observed, no invitation sent | Workspace team list, link creation, role selection and revocation are implemented. Email delivery, optional message, role editing and recipient acceptance verification remain open. |
| [ ] | Settings, Billing summary, payment method and invoices | Observed | `/app#billing` has plan entry points and Stripe portal. Verify setup, payments and invoice history in sandbox. |
| [ ] | Account menu, agent switcher, new agent and sign-out | Menu observed, creation workflow pending | Workspace switcher and sign-out exist. Multi-agent management missing. |
| [ ] | Support control and contextual help | Entry point observed | `/app#help` exists. Compare support dialogs and escalation. |
| [ ] | Browser practice, microphone prompt, typing and return to setup | Entry points observed, live voice exchange pending | `/app#simulator` verified for text retrieval and repeatable saved answer checks. Voice practice and an in-session transcript are implemented behind the provider-use flag. Spoken verification, sharing and recorded evaluations remain open. |
| [ ] | Mobile navigation, menus, empty/loading/error states and accessibility | Some desktop states observed, mobile inventory pending | Validate every implemented surface at mobile breakpoints and with keyboard navigation. |

## Coverage limits

This inventory covers the routes and surfaces discovered so far. Linked article and landing-page children, empty-account-dependent states, paid/provider workflows, and settings that require external changes are still open. Page groups explicitly marked pending must be expanded into individual entries before a claim of exhaustive page coverage.
