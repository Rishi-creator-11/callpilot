"""CallPilot protected internal demo; no live booking or telephony is activated."""
from __future__ import annotations
import os, sqlite3, uuid, hmac, re
from pathlib import Path
from html import escape
from datetime import datetime, timezone
from fastapi import FastAPI, Header, HTTPException, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from app.common import authorize, MAX_CALL_SECONDS, PRICE, MONEY, NO_PRICE, UNKNOWN

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "callpilot.db"
app = FastAPI(title="CallPilot", version="0.2.0")

def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db

with connect() as db:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS businesses(
      id TEXT PRIMARY KEY, name TEXT NOT NULL, industry TEXT NOT NULL,
      agent_name TEXT NOT NULL DEFAULT 'Alex',
      greeting TEXT NOT NULL DEFAULT 'Hello, I am the AI assistant. How can I help?',
      voice_provider TEXT NOT NULL DEFAULT 'standard',
      voice_id TEXT NOT NULL DEFAULT '',
      voice_owner_verified INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS knowledge(
      id TEXT PRIMARY KEY, business_id TEXT NOT NULL REFERENCES businesses(id),
      question TEXT NOT NULL, answer TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS booking_requests(
      id TEXT PRIMARY KEY, business_id TEXT NOT NULL REFERENCES businesses(id),
      name TEXT NOT NULL, phone TEXT NOT NULL, service TEXT NOT NULL,
      requested_time TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'requested');
    """)

def uid(): return str(uuid.uuid4())
def ts(): return datetime.now(timezone.utc).isoformat()
def business_or_404(db, business_id):
    row = db.execute("SELECT * FROM businesses WHERE id=?", (business_id,)).fetchone()
    if not row: raise HTTPException(404, "Business not found")
    return row

class BusinessInput(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    industry: str = Field(default="General", max_length=80)

class AgentInput(BaseModel):
    name: str = Field(default="Alex", max_length=80)
    greeting: str = Field(min_length=10, max_length=500)
    voice_provider: str = Field(default="standard", pattern="^(standard|elevenlabs)$")
    voice_id: str = Field(default="", max_length=180)
    owner_verified: bool = False

class KnowledgeInput(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    answer: str = Field(min_length=2, max_length=2500)
    approved: bool = False

class QuestionInput(BaseModel):
    question: str = Field(min_length=1, max_length=1000)

class BookingInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=3, max_length=100)
    service: str = Field(min_length=1, max_length=200)
    requested_time: str = Field(min_length=4, max_length=100)

@app.get("/health")
def health(): return {"status":"ok","mode":"internal-demo"}

@app.get("/", response_class=HTMLResponse)
def home():
    return marketing_page()


@app.get("/pricing", response_class=HTMLResponse)
def pricing():
    from app.billing import PLANS, SETUP_FEE_CENTS

    columns = "".join(f'<th scope="col">{plan["name"]}</th>' for plan in PLANS.values())
    rows = (
        ("Monthly price", [f'${plan["monthly_cents"] / 100:,.2f}' for plan in PLANS.values()]),
        ("Included minutes", [f'{plan["included_minutes"]:,}' for plan in PLANS.values()]),
        ("One-time setup fee", [f"${SETUP_FEE_CENTS / 100:,.2f}"] * len(PLANS)),
        ("Maximum call duration", [f"{MAX_CALL_SECONDS // 60} minutes"] * len(PLANS)),
        ("Automatic overage charges", ["None"] * len(PLANS)),
    )
    comparison = "".join(f'<tr><th scope="row">{label}</th>'
                         + "".join(f"<td>{value}</td>" for value in values) + "</tr>" for label, values in rows)
    extras = f"""
<section class="band shaded"><div class="wrap"><p class="eyebrow">Plan comparison</p><h2>Find your monthly fit.</h2>
<div class="table-scroll" tabindex="0" role="region" aria-label="CallPilot plan comparison">
<table class="plan-compare"><thead><tr><th scope="col">Plan details</th>{columns}</tr></thead>
<tbody>{comparison}</tbody></table></div></div></section>
<section class="band"><div class="wrap faq"><h2>Billing questions</h2>
<details><summary>When is the setup fee collected?</summary><p>The setup fee is collected with your first subscription payment.</p></details>
<details><summary>Where can I manage my subscription?</summary><p>Open Billing &amp; plans in your workspace to see subscription status and manage your payment details when billing is connected.</p></details>
<details><summary>Where can I review the full terms?</summary><p>See the terms for billing, cancellation, usage, and recording.</p><a href="/legal">Read the terms</a></details>
</div></section>"""
    return marketing_page("pricing", "Pricing", after_section=extras)


@app.get("/how-it-works", response_class=HTMLResponse)
def how_it_works():
    return marketing_page("how-it-works", "How it works")


@app.get("/faq", response_class=HTMLResponse)
def faq():
    return marketing_page("faq", "Frequently asked questions")


@app.get("/voice-demo", response_class=HTMLResponse, include_in_schema=False)
def voice_demo_page():
    from app.demo_links import PRIVATE_HEADERS, marketing_voice_path

    live = marketing_voice_path()
    action = (
        '<a class="button" href="/voice-demo/start">Open live demo ↗</a>'
        if live else '<a class="button" href="/#call-example">Explore the call example ↗</a>'
    )
    availability = (
        "A live demo is available. You’ll choose whether to share your microphone before speaking."
        if live else "Live public sessions are currently unavailable. Explore the sample call or try voice practice in your workspace."
    )
    content = f"""
<section class="hero-shell"><div class="wrap demo-landing">
 <div><p class="eyebrow">Meet the receptionist</p><h1>Start with a conversation.</h1>
  <p class="lead">{availability}</p>
  <div class="actions">{action}<a class="button quiet" href="/app#simulator">Practice with your agent</a></div>
 </div>
 <article class="example demo-preview" aria-label="Voice demo overview">
  <div class="example-head"><span class="avatar" aria-hidden="true">✦</span>
   <div><strong>CallPilot voice preview</strong><p>One minute in the browser</p></div></div>
  <div class="example-bar">Approved answers only</div>
  <div class="demo"><h2>Try a question you hear every day.</h2>
   <p class="bubble">What kinds of jobs does the team handle?</p>
   <p class="bubble">When is the business open?</p>
   <p class="outcome">A demo session won’t create appointments, place orders, or call a phone number.</p>
  </div>
 </article>
</div></section>
<section class="band"><div class="wrap"><h2>Bring your own business details.</h2>
<p class="lead">Open your workspace to hear a saved greeting, check approved answers, and adjust your receptionist before connecting a phone.</p>
<a class="button dark" href="/app?mode=signup">Build your agent ↗</a></div></section>
"""
    return HTMLResponse(marketing_page(title="Voice demo", content=content), headers=PRIVATE_HEADERS)


@app.get("/voice-demo/start", include_in_schema=False)
def open_marketing_voice_demo():
    from app.demo_links import PRIVATE_HEADERS, marketing_voice_path

    return RedirectResponse(marketing_voice_path() or "/voice-demo", headers=PRIVATE_HEADERS)


PRODUCT_PAGES = {
    "answering-services": (
        "Give callers a helpful first answer", "Set up a receptionist that knows your business and can take a message.",
        "Choose a greeting and approve the business information your receptionist can share. Enable callbacks so your team can follow up when a caller needs personal help.",
        ("Set your receptionist’s greeting in Launch Studio.", "Add and approve answers to common questions.",
         "Try your answers before connecting a number, then confirm routing with a controlled call."),
        "A caller asks about service hours", "The receptionist checks the approved hours and can collect a callback request if the caller needs more help.",
        "/app#studio", "Set up your receptionist",
    ),
    "local-phone-numbers": (
        "Connect a number for your business", "Bring your existing public number into your CallPilot setup with forwarding.",
        "Use your connected CallPilot number directly or forward calls to it from your existing line. Eligible workspaces can reserve a U.S. number by area code. You can also connect a supported existing number.",
        ("Review your connected number in Usage &amp; phone.", "Choose how your carrier forwards calls from your existing line.",
         "Check the greeting, fallback destination, and what happens when a caller hangs up."),
        "An unanswered call reaches your receptionist", "A carrier can forward an unanswered call to your connected CallPilot number when that forwarding option is available.",
        "/app#usage", "Review your phone setup",
    ),
    "call-routing": (
        "Decide where calls should go", "Set screening rules before a caller reaches your receptionist.",
        "CallPilot evaluates saved rules in order. Match a caller-number prefix, calls without caller ID, or times outside your booking hours.",
        ("Open Channels and add a rule with the conditions you want.", "Choose the operator, an enabled specialist, your human fallback, or decline the call.",
         "Activate CallPilot admission for the business number and review recent routing decisions."),
        "An after-hours call gets a defined route", "A saved rule can forward calls outside your hours to your configured backup line.",
        "/app#channels", "Open call routing",
    ),
    "receptionist-workers": (
        "Give each caller the right specialist", "Set a role, approved knowledge, and an intake form for different kinds of calls.",
        "Keep general questions with the receptionist. Add a specialist for focused conversations such as sales intake or service questions.",
        ("Create a worker and explain when it should handle the call.", "Choose what the specialist should ask and mark required questions.",
         "Enable runtime specialists in Channels and review the saved intake after a call."),
        "A new customer explains a job", "A specialist collects the details your team needs and saves the confirmed answers to the call.",
        "/app#workers", "Open Workers",
    ),
    "business-phone-number": (
        "Start with a local area code", "Choose a U.S. area code for your CallPilot number or connect an existing line.",
        "Eligible workspaces can ask the phone provider to reserve a number. Availability is confirmed during reservation, and an owner activates incoming calls separately.",
        ("Enter the area code you want to use.", "Open Channels to request a number if your workspace is eligible.",
         "Activate the line with a paid plan, or set forwarding through your current carrier."),
        "A number for your service area", "Start with your preferred area code. The provider confirms the exact number when it accepts the reservation.",
        "/app#channels", "Open phone channels",
    ),
    "lead-capture": (
        "Capture leads while you work", "Give callers a way to leave a clear callback request.",
        "CallPilot can ask what the caller needs, confirm their contact details, and record an approved callback request for your team.",
        ("Review your business information and greeting.", "Enable callback capture in Launch Studio.",
         "Review captured requests from Calls &amp; leads."),
        "A caller requests a quote", "The receptionist confirms the caller’s name, number, and what they need before saving the request.",
        "/app#studio", "Open Launch Studio",
    ),
    "appointment-scheduling": (
        "Make room for your next customer", "Book visits around your real calendar and business hours.",
        "Connect Google Calendar, set when you take appointments, and enable booking after reviewing your configuration.",
        ("Connect Google Calendar from Appointments.", "Set weekly booking hours and date exceptions.",
         "Enable voice booking, then manage confirmed appointments, changes and cancellations from the dashboard."),
        "A caller requests a Friday visit", "The receptionist checks the slot and confirms the booking after a calendar event is created.",
        "/app#appointments", "Open appointments",
    ),
    "product-orders": (
        "Help callers find the right product", "Answer catalog questions and collect order requests for your team.",
        "Choose recorded or Shopify inventory for each approved SKU. Your team can approve requests, share a Shopify checkout link, and check payment and fulfillment status.",
        ("Add approved SKUs and choose the source for stock.", "Connect Shopify if you want live stock for an approved item.",
         "Approve a request and create its Shopify checkout link from Order requests."),
        "A caller asks for a product", "The receptionist checks the approved catalog and can save a pending request for your team to review.",
        "/app#studio", "Open catalog setup",
    ),
    "call-history": (
        "See what happened after the ring", "Find the conversations that need a follow-up.",
        "Review call details, available transcripts and recordings, and the context your team needs for the next conversation.",
        ("Open Calls &amp; leads.", "Search or filter calls by date and review individual conversations.",
         "Record the follow-up status and notes for your team."),
        "A missed opportunity becomes a next step", "A teammate reviews the summary, checks the caller’s request, and records the follow-up.",
        "/app#calls", "Open Calls &amp; leads",
    ),
    "phone-setup": (
        "Bring CallPilot onto your phone line", "Set up a managed number and check where calls go.",
        "Keep your public business number and use your carrier’s forwarding options with a connected CallPilot number.",
        ("Open Usage &amp; phone to review your number.", "Link your managed number and configure a fallback when available.",
         "Confirm the forwarding behavior with your carrier before sending customer calls."),
        "Your existing line stays familiar", "Customers dial the number they know, and your carrier forwards calls according to the settings you choose.",
        "/app#usage", "Open phone setup",
    ),
    "crm-integrations": (
        "Find the right contact before following up", "Connect your HighLevel sub-account for staff contact lookup.",
        "Owners and admins can link a HighLevel private integration and search that sub-account by the customer's exact email or phone.",
        ("Create a sub-account token with locations.readonly and contacts.readonly.",
         "Open Connections, choose HighLevel, and enter the token and sub-account ID.",
         "Look up the contact using details your customer has already provided."),
        "Your team prepares a callback", "A teammate finds the customer's existing HighLevel record before returning the call.",
        "/app#connections", "Open Connections",
    ),
}


def render_product_page(product_page: str):
    heading, lead, introduction, steps, sample_title, sample, destination, action = PRODUCT_PAGES[product_page]
    sequence = "".join(f"<li>{step}</li>" for step in steps)
    content = f"""
<section class="band"><div class="wrap">
<div class="section-head"><p class="eyebrow">CallPilot capabilities</p><h1>{heading}.</h1>
<p class="lead">{lead}</p><a class="button dark" href="{destination}">{action} →</a></div>
<div class="detail-layout"><div><h2>How it works</h2><p>{introduction}</p><ol>{sequence}</ol></div>
<article class="card"><p class="eyebrow">Example workflow</p><h3>{sample_title}</h3><p>{sample}</p></article></div>
</div></section>"""
    if product_page == "business-phone-number":
        content += """
<section class="band shaded"><div class="wrap"><h2>Choose your area code.</h2>
<form action="/app" method="get" class="resource-search">
 <label for="area-code">U.S. area code</label>
 <input id="area-code" name="area_code" pattern="[2-9][0-9]{2}" inputmode="numeric"
        maxlength="3" placeholder="212" required>
 <button type="submit" class="button dark">Continue to Channels</button>
</form>
<p class="price-note">CallPilot saves this preference on your device. The provider confirms number availability during reservation.</p>
</div></section>"""
    return marketing_page(title=heading, content=content)


def marketing_page(section_id=None, title=None, content=None, after_section=""):
    from app.billing import PLANS, SETUP_FEE_CENTS

    plans = [
        '<article class="card plan">'
        f'<h3>{plan["name"]}</h3><p class="price">${plan["monthly_cents"] // 100}<span> / month</span></p>'
        f'<p>{plan["included_minutes"]:,} included calling minutes per month.</p>'
        '<a class="button dark" href="/app#billing">View billing options →</a></article>'
        for plan in PLANS.values()
    ]
    html = (ROOT / "static" / "marketing.html").read_text()
    if section_id or content:
        header, main = html.split('<main id="main">', 1)
        if section_id:
            section = re.search(r'<section [^>]*id="' + re.escape(section_id) + r'".*?</section>', main, re.DOTALL)
            content = section.group().replace("<h2>", "<h1>", 1).replace("</h2>", "</h1>", 1) + after_section
        _, footer = main.split("</main>", 1)
        header = header.replace("<title>CallPilot | An AI front desk for your business</title>",
                                f"<title>{title} | CallPilot</title>")
        html = header + '<main id="main" class="detail-page">' + content + "</main>" + footer
    return html.replace("{{plan_cards}}", "".join(plans)).replace("{{setup_fee}}", f"${SETUP_FEE_CENTS // 100}")

def product_endpoint(slug):
    def endpoint():
        return render_product_page(slug)
    return endpoint


for slug in PRODUCT_PAGES:
    app.add_api_route("/" + slug, product_endpoint(slug), response_class=HTMLResponse,
                      name=slug, include_in_schema=False)


GUIDES = {
    "build-your-receptionist": (
        "Build your first receptionist", "Agent setup",
        "Set your greeting and decide what information callers should hear.",
        (
            ("Create the workspace", "Start with the name your customers recognize. Add your public site if you want a starting draft."),
            ("Approve your knowledge", "Read the imported text and remove stale policies, internal information, and quotes that have not been approved."),
            ("Review the agent configuration", "Describe your greeting and conversation style in Agent builder. Review and save the changes, then sync the managed assistant when ready."),
        ),
        "/app#builder",
    ),
    "booking-hours": (
        "Configure appointment booking", "Calendar",
        "Set business hours and exceptions before giving your receptionist booking access.",
        (
            ("Connect the calendar", "Open Appointments and authorize the Google account whose primary calendar should hold the bookings."),
            ("Set hours and exceptions", "Choose the business time zone, weekly hours and any closures in Business settings. Keep availability blocked in Google Calendar whenever your team is unavailable."),
            ("Review confirmed bookings", "Use Appointments to check a time, create a booking, or cancel a verified CallPilot event. Staff can continue reviewing existing events directly in Google Calendar after disconnecting."),
        ),
        "/app#appointments",
    ),
    "forward-your-business-number": (
        "Forward your business number", "Phone setup",
        "Keep the number on your website while routing selected calls to your receptionist.",
        (
            ("Find your CallPilot destination", "Open Usage & phone and confirm which managed number is linked to your receptionist."),
            ("Choose your forwarding behavior", "Use your carrier’s controls for unconditional or conditional forwarding where available. Consider when staff should answer first and what should happen outside working hours."),
            ("Confirm with a controlled call", "Check the greeting and fallback from a separate line. Verify that the carrier forwards the calls you intended to send."),
        ),
        "/app#usage",
    ),
    "connect-highlevel": (
        "Connect HighLevel for contact lookup", "CRM",
        "Give authorized staff a way to look up a customer in the correct sub-account.",
        (
            ("Create a scoped token", "Use the intended HighLevel sub-account and allow locations.readonly and contacts.readonly."),
            ("Save the connection", "In Connections, provide the sub-account ID and private integration token. CallPilot stores the credential on the server."),
            ("Find a contact", "Search by the customer’s exact email or phone. Verify any details you need before using them in a follow-up."),
        ),
        "/app#connections",
    ),
    "shopify-stock": (
        "Use Shopify stock for an approved SKU", "E-commerce",
        "Connect a merchant-owned app and choose which products may use live inventory.",
        (
            ("Install a read-only app", "In your Shopify organization, install the app on the target store with read_products and read_inventory."),
            ("Connect the store", "Enter the myshopify.com domain and app credentials in Connections. Check that the SKU has one active variant with inventory tracking enabled."),
            ("Approve the product", "Choose Shopify inventory for the SKU in Launch Studio, approve the product, and sync the managed assistant after enabling catalog lookup. Order requests still go to staff review."),
        ),
        "/app#connections",
    ),
}


@app.get("/resources", response_class=HTMLResponse, include_in_schema=False)
def resources(q: str = Query(default="", max_length=120)):
    query = " ".join(q.split())
    cards = []
    for slug, (title, category, summary, sections, _) in GUIDES.items():
        searchable = " ".join([title, category, summary, *(word for section in sections for word in section)]).casefold()
        if all(word in searchable for word in query.casefold().split()):
            cards.append(f'<article class="card"><p class="eyebrow">{category}</p><h3>{title}</h3>'
                         f'<p>{summary}</p><a class="card-link" href="/resources/{slug}">Read guide →</a></article>')
    clear = '<a href="/resources">Clear search</a>' if query else ""
    results = f'<div class="grid">{"".join(cards)}</div>' if cards else "<p>No guides match your search.</p>"
    content = f"""
<section class="band"><div class="wrap"><p class="eyebrow">CallPilot resources</p>
<h1>Put your front desk to work.</h1><p class="lead">Practical guides for setup, routing, bookings and integrations.</p>
<form class="resource-search" action="/resources" method="get" role="search"><label for="guide-query">Find a guide</label>
<input type="search" id="guide-query" name="q" value="{escape(query, quote=True)}" maxlength="120">
<button class="button dark" type="submit">Search</button>{clear}</form>
<p class="price-note">{len(cards)} {"guide" if len(cards) == 1 else "guides"}</p>{results}</div></section>"""
    return marketing_page(title="Setup guides", content=content)


@app.get("/resources/{slug}", response_class=HTMLResponse, include_in_schema=False)
def guide(slug: str):
    if slug not in GUIDES:
        raise HTTPException(404, "Guide not found")
    title, category, summary, sections, destination = GUIDES[slug]
    steps = "".join(f"<h2>{heading}</h2><p>{body}</p>" for heading, body in sections)
    content = f"""
<section class="band guide"><div class="wrap"><article>
<p><a href="/resources">← All setup guides</a></p><p class="eyebrow">{category}</p><h1>{title}</h1>
<p class="lead">{summary}</p>{steps}<a class="button dark" href="{destination}">Open the workspace →</a>
</article></div></section>"""
    return marketing_page(title=title, content=content)


INTEGRATIONS = {
    "google-calendar": (
        "Google Calendar", "Scheduling", "Check availability and manage appointments on your connected primary calendar.",
        "Connect from Appointments, then review your time zone, regular hours, and date exceptions.",
        "/resources/booking-hours", "/app#appointments",
    ),
    "highlevel": (
        "HighLevel", "CRM", "Look up contacts from an authorized HighLevel sub-account by exact email or phone.",
        "Create a private integration token with contacts.readonly and locations.readonly, then enter the token and sub-account ID.",
        "/resources/connect-highlevel", "/app#connections",
    ),
    "shopify": (
        "Shopify", "Commerce", "Look up live stock for active variants and approved catalog SKUs.",
        "Connect a merchant-owned app with read_products and read_inventory. Approve the individual SKUs your assistant may use.",
        "/resources/shopify-stock", "/app#connections",
    ),
    "vapi": (
        "Vapi", "Voice", "Connect a managed phone assistant and review the number that receives your calls.",
        "Link your assistant to the business, configure the managed number, and confirm carrier forwarding and fallback behavior.",
        "/resources/forward-your-business-number", "/app#channels",
    ),
}


@app.get("/business-productivity-ai", response_class=HTMLResponse, include_in_schema=False)
def integration_directory():
    cards = "".join(f'<article class="card"><p class="eyebrow">{category}</p><h3>{name}</h3><p>{summary}</p>'
                    f'<a class="card-link" href="/integrations/{slug}">View setup →</a></article>'
                    for slug, (name, category, summary, *_rest) in INTEGRATIONS.items())
    return marketing_page(title="Integrations", content=f"""
<section class="band"><div class="wrap"><p class="eyebrow">CallPilot integrations</p>
<h1>Connect the tools your team uses.</h1><p class="lead">Choose the calendar, contact, catalog and phone services you want to connect.</p>
<div class="feature-list">{cards}</div></div></section>""")


@app.get("/integrations/{slug}", response_class=HTMLResponse, include_in_schema=False)
def integration_details(slug: str):
    if slug not in INTEGRATIONS:
        raise HTTPException(404, "Integration not found")
    name, category, summary, steps, guide_url, workspace = INTEGRATIONS[slug]
    return marketing_page(title=name + " integration", content=f"""
<section class="band guide"><div class="wrap"><article>
<p><a href="/business-productivity-ai">← All integrations</a></p><p class="eyebrow">{category}</p>
<h1>Connect {name}.</h1><p class="lead">{summary}</p>
<h2>Set up the connection</h2><p>{steps}</p>
<div class="actions"><a class="button dark" href="{workspace}">Open the workspace →</a>
<a href="{guide_url}">Read the setup guide</a></div></article></div></section>""")


@app.get("/support", response_class=HTMLResponse, include_in_schema=False)
def support():
    return marketing_page(title="Help center", content="""
<section class="band"><div class="wrap"><p class="eyebrow">CallPilot support</p><h1>How can we help?</h1>
<p class="lead">Find setup guides and answers for your account, assistant, phone and connected tools.</p>
<form class="resource-search" action="/resources" method="get" role="search"><label for="support-query">Search setup guides</label>
<input type="search" id="support-query" name="q" maxlength="120"><button class="button dark" type="submit">Search</button></form>
<div class="grid">
<article class="card"><h3>Set up your assistant</h3><p>Start with your identity, greeting, and approved information.</p><a href="/resources/build-your-receptionist">Read the setup guide</a></article>
<article class="card"><h3>Manage connected tools</h3><p>Connect Calendar, HighLevel, Shopify, or your phone.</p><a href="/business-productivity-ai">Browse integrations</a></article>
<article class="card"><h3>Plans and usage</h3><p>Compare minute allowances and find billing information.</p><a href="/pricing">View pricing</a></article>
</div><p class="price-note"><a href="/faq">Frequently asked questions</a> · <a href="/app#help">Help inside your workspace</a></p>
</div></section>""")


@app.get("/about", response_class=HTMLResponse, include_in_schema=False)
def about():
    return marketing_page(title="About CallPilot", content="""
<section class="band"><div class="wrap"><p class="eyebrow">About CallPilot</p><h1>Give callers a clear next step.</h1>
<p class="lead">CallPilot gives businesses a front desk for calls, appointment requests, and customer questions.</p>
<div class="grid">
<article class="card"><h3>Use information you approve</h3><p>Review imported business information before your assistant uses it on a call.</p></article>
<article class="card"><h3>Keep your workflow</h3><p>Connect the calendar and phone tools your team already uses.</p></article>
<article class="card"><h3>Review what happened</h3><p>Find available call transcripts, follow-up notes, and order requests in your workspace.</p></article>
</div></div></section>""")


@app.get("/news", response_class=HTMLResponse, include_in_schema=False)
def product_updates():
    return marketing_page(title="Product updates", content="""
<section class="band"><div class="wrap"><p class="eyebrow">What’s new</p><h1>CallPilot product updates.</h1>
<p class="lead">Read about changes to the assistant, workspace, and connected tools.</p>
<div class="grid">
<article class="card"><p class="eyebrow"><time datetime="2026-11-07">November 7, 2026</time></p><h3>An assistant-first workspace</h3><p>Open your conversation next to the dashboard, revisit earlier proposals, and review identity settings in their own tabs.</p><a class="card-link" href="/resources/build-your-receptionist">Explore assistant setup →</a></article>
<article class="card"><p class="eyebrow"><time datetime="2026-11-07">November 7, 2026</time></p><h3>Connections and orders</h3><p>Configure Shopify and HighLevel, choose live inventory for approved products, and review order requests in their own queue.</p><a class="card-link" href="/business-productivity-ai">Browse integrations →</a></article>
<article class="card"><p class="eyebrow"><time datetime="2026-11-07">November 7, 2026</time></p><h3>Manage appointment bookings</h3><p>Check available times, create appointments, and cancel verified CallPilot events from your workspace.</p><a class="card-link" href="/resources/booking-hours">Read the booking guide →</a></article>
</div></div></section>""")


@app.get("/api/businesses", dependencies=[Depends(authorize)])
def businesses():
    with connect() as db: return [dict(r) for r in db.execute("SELECT * FROM businesses ORDER BY created_at DESC")]

@app.post("/api/businesses", dependencies=[Depends(authorize)])
def create_business(body: BusinessInput):
    id = uid()
    with connect() as db:
        db.execute("INSERT INTO businesses(id,name,industry,created_at) VALUES(?,?,?,?)",(id,body.name,body.industry,ts()))
        db.commit()
    return {"id":id,"name":body.name}

@app.put("/api/businesses/{bid}/agent", dependencies=[Depends(authorize)])
def agent(bid: str, body: AgentInput):
    if body.voice_provider=="elevenlabs" and (not body.owner_verified or not body.voice_id):
        raise HTTPException(400,"Verified voice-owner consent and voice ID required")
    with connect() as db:
        business_or_404(db,bid)
        db.execute("UPDATE businesses SET agent_name=?,greeting=?,voice_provider=?,voice_id=?,voice_owner_verified=? WHERE id=?",
            (body.name,body.greeting,body.voice_provider,body.voice_id,int(body.owner_verified),bid))
        db.commit()
    return {"saved":True,"voice_is_live":False}

@app.get("/api/businesses/{bid}/knowledge", dependencies=[Depends(authorize)])
def knowledge(bid: str):
    with connect() as db:
        business_or_404(db,bid)
        return [dict(x) for x in db.execute("SELECT * FROM knowledge WHERE business_id=?",(bid,))]

@app.post("/api/businesses/{bid}/knowledge", dependencies=[Depends(authorize)])
def add_knowledge(bid: str, body: KnowledgeInput):
    if MONEY.search(body.answer): raise HTTPException(400,"Price or monetary answers cannot be stored")
    with connect() as db:
        business_or_404(db,bid)
        id=uid()
        db.execute("INSERT INTO knowledge VALUES(?,?,?,?,?)",(id,bid,body.question,body.answer,int(body.approved)))
        db.commit()
    return {"id":id,"approved":body.approved}

@app.post("/api/businesses/{bid}/simulate", dependencies=[Depends(authorize)])
def simulate(bid: str, body: QuestionInput):
    with connect() as db:
        business_or_404(db,bid)
        if PRICE.search(body.question): return {"answer":NO_PRICE,"source":"policy","human_needed":True}
        rows=db.execute("SELECT question,answer FROM knowledge WHERE business_id=? AND approved=1",(bid,)).fetchall()
        for row in rows:
            if row["question"].strip().casefold()==body.question.strip().casefold():
                if MONEY.search(row["answer"]): return {"answer":NO_PRICE,"source":"policy","human_needed":True}
                return {"answer":row["answer"],"source":"approved_knowledge","human_needed":False}
    return {"answer":UNKNOWN,"source":"unknown","human_needed":True}

@app.post("/api/businesses/{bid}/booking-requests", dependencies=[Depends(authorize)])
def booking_request(bid: str, body: BookingInput):
    with connect() as db:
        business_or_404(db,bid)
        id=uid()
        db.execute("INSERT INTO booking_requests VALUES(?,?,?,?,?,?,'requested')",
                   (id,bid,body.name,body.phone,body.service,body.requested_time))
        db.commit()
    return {"id":id,"status":"requested","confirmed":False}

@app.get("/api/businesses/{bid}/voice-preview", dependencies=[Depends(authorize)])
def voice_preview(bid: str):
    with connect() as db:
        b=business_or_404(db,bid)
        faq=db.execute("SELECT question,answer FROM knowledge WHERE business_id=? AND approved=1",(bid,)).fetchall()
        return {"name":b["agent_name"],"firstMessage":b["greeting"],"voice":{"provider":b["voice_provider"],"voiceId":b["voice_id"]},
                "system_prompt":"You are an AI receptionist. Never quote prices or estimates. Never invent information. Escalate unknown questions. Only use approved FAQ facts.",
                "approved_faqs":[dict(x) for x in faq],"not_active":True}

# Cloud persistence is enabled only after server-side Supabase credentials are configured.
from app.cloud import router as cloud_router
app.include_router(cloud_router)

from app.vapi import router as vapi_router
app.include_router(vapi_router)

# Startup diagnostics only log status codes and flags; they never log credentials.
@app.on_event("startup")
def check_integrations_on_boot():
    import logging
    import httpx
    log=logging.getLogger("uvicorn.error")
    db_url=os.getenv("SUPABASE_URL","").rstrip("/")
    db_secret=os.getenv("SUPABASE_SECRET_KEY","")
    vapi_key=os.getenv("VAPI_API_KEY","")
    log.info("callpilot_voice_semantic_webhook ready=%s credential_id=%s webhook_token=%s",
             bool(os.getenv("VAPI_CREDENTIAL_ID") and os.getenv("VAPI_WEBHOOK_TOKEN")),
             bool(os.getenv("VAPI_CREDENTIAL_ID")),bool(os.getenv("VAPI_WEBHOOK_TOKEN")))
    log.info("callpilot_readiness google_client=%s google_secret=%s google_encryption=%s stripe_test_key=%s stripe_webhook_secret=%s stripe_price_id=%s stripe_billing_enabled=%s public_supabase_key=%s",
             bool(os.getenv("GOOGLE_OAUTH_CLIENT_ID")), bool(os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")),
             bool(os.getenv("GOOGLE_TOKEN_ENCRYPTION_KEY")),
             os.getenv("STRIPE_SECRET_KEY","").startswith("sk_test_"),bool(os.getenv("STRIPE_WEBHOOK_SECRET")),
             bool(os.getenv("STRIPE_PRICE_ID")),os.getenv("STRIPE_BILLING_ENABLED")=="true",
             bool(os.getenv("SUPABASE_PUBLISHABLE_KEY")))
    log.info("callpilot_integrations configured supabase_url=%s supabase_secret=%s vapi_key=%s admin_token=%s",
             bool(db_url),bool(db_secret),bool(vapi_key),bool(os.getenv("ADMIN_TOKEN")))
    if db_url and db_secret:
        try:
            with httpx.Client(timeout=5) as c:
                r=c.get(db_url+"/rest/v1/organizations",
                        params={"select":"id","limit":"1"},headers={"apikey":db_secret})
            log.info("callpilot_supabase_probe http_status=%s connected=%s",r.status_code,r.status_code==200)
        except Exception as e:
            log.warning("callpilot_supabase_probe failed error_type=%s",type(e).__name__)
    # One-time semantic rollout: index a small number of pre-existing approved FAQs.
    # Existing indexed facts are skipped on later deployments.
    if db_url and db_secret:
        try:
            from app.cloud import api as cloud_api
            from app.semantic import reindex_approved, retrieve
            orgs=cloud_api("organizations",params={"select":"id","limit":5})
            for organization in orgs:
                result=reindex_approved(organization["id"])
                log.info("callpilot_semantic_backfill indexed=%s",result["indexed"])
                if len(orgs)==1:
                    fact,metadata=retrieve(organization["id"],"Can I buy half container?")
                    log.info("callpilot_semantic_smoke matched=%s reason=%s score=%s",
                             bool(fact),metadata.get("reason"),metadata.get("similarity","n/a"))
                    minimum_fact,minimum_meta=retrieve(organization["id"],"Can I buy 1 product?")
                    log.info("callpilot_minimum_order_smoke matched=%s policy=%s",
                             bool(minimum_fact),minimum_meta.get("reason")=="approved_policy_entailment")
                    unrelated,negative=retrieve(organization["id"],"Do you repair cars and motorcycles?")
                    log.info("callpilot_semantic_negative unknown=%s reason=%s score=%s",
                             unrelated is None,negative.get("reason"),negative.get("similarity","n/a"))
        except Exception as e:
            log.warning("callpilot_semantic_rollout error_type=%s",type(e).__name__)
    # Keep already-provisioned managed assistants on the same global voice policy.
    if db_url and db_secret and vapi_key:
        try:
            from app.cloud import api as cloud_api
            from app.call_intel import refresh_receptionist_prompt
            for org in cloud_api("organizations",params={"select":"id","limit":100}):
                try:
                    outcome=refresh_receptionist_prompt(org["id"])
                    log.info("callpilot_global_voice_policy updated=%s reason=%s",
                             outcome["updated"],outcome["reason"])
                except Exception as exc:
                    log.warning("callpilot_global_voice_policy_failed org=%s error_type=%s",
                                org["id"],type(exc).__name__)
        except Exception as exc:
            log.warning("callpilot_global_voice_policy_rollout_failed error_type=%s",type(exc).__name__)
    # Import initial historical calls once for clearly matched, configured workspaces.
    # A private assistant is linked only when its name uniquely matches the business.
    if db_url and db_secret and vapi_key:
        try:
            from app.call_intel import sync_org_calls
            from app.cloud import api as cloud_api
            for org in cloud_api("organizations",params={"select":"id","limit":5}):
                existing=cloud_api("call_insights",params={
                    "organization_id":"eq."+org["id"],"select":"id","limit":1})
                if not existing:
                    result=sync_org_calls(org["id"],limit=20)
                    log.info("callpilot_call_intelligence_initial_sync imported=%s",result["synced"])
                from app.call_intel import configure_analysis, agent_config
                if agent_config(org["id"]).get("provider_agent_id"):
                    try:
                        from app.usage import discover_linked_business_phone
                        phone_link=discover_linked_business_phone(org["id"])
                        log.info("callpilot_existing_phone_link verified=%s reason=%s",
                                 phone_link.get("linked"),phone_link.get("reason"))
                    except Exception as phone_error:
                        log.warning("callpilot_existing_phone_link failed=%s",type(phone_error).__name__)
                    try:
                        configured=configure_analysis(org["id"])
                        log.info("callpilot_call_analysis_enabled=%s reused=%s",
                                 configured["enabled"],configured.get("existing",False))
                        try:
                            from app.call_intel import disable_knowledge_filler
                            quiet=disable_knowledge_filler(org["id"])
                            log.info("callpilot_voice_filler_fix updated=%s reason=%s",
                                     quiet["updated"],quiet["reason"])
                            from app.call_intel import apply_call_duration_policy
                            cap=apply_call_duration_policy(org["id"])
                            log.info("callpilot_4min_policy configured=%s changed=%s",
                                     cap.get("configured"),cap.get("changed"))
                        except Exception as voice_error:
                            log.warning("callpilot_voice_filler_fix failed=%s",type(voice_error).__name__)
                        # Backfill analysis once on existing calls that predate the new feature.
                        from app.call_intel import request_call_analysis
                        old_calls=cloud_api("call_insights",params={
                            "organization_id":"eq."+org["id"],
                            "analysis_requested_at":"is.null","lead_source":"eq.unreviewed",
                            "select":"provider_call_id","limit":3})
                        for item in old_calls:
                            try:
                                outcome=request_call_analysis(org["id"],item["provider_call_id"])
                                log.info("callpilot_historical_analysis_queued=%s",outcome.get("queued"))
                            except Exception as inner:
                                log.warning("callpilot_historical_analysis_blocked error_type=%s",type(inner).__name__)
                        # Reconcile completed analysis on calls that were queued earlier.
                        # Never log transcript, customer identity, or recording.
                        from app.call_intel import vapi as vapi_api, persist as save_call
                        queued=cloud_api("call_insights",params={
                            "organization_id":"eq."+org["id"],
                            "analysis_requested_at":"not.is.null","lead_source":"eq.unreviewed",
                            "select":"provider_call_id","limit":3})
                        for row in queued:
                            try:
                                detail=vapi_api("GET","/call/"+row["provider_call_id"])
                                artifacts=(detail.get("artifact") or {}).get("structuredOutputs") or {}
                                present=bool(artifacts)
                                log.info("callpilot_queued_analysis_provider_output_available=%s",present)
                                if present:
                                    save_call(org["id"],detail)
                            except Exception as err:
                                log.warning("callpilot_analysis_reconcile_error_type=%s",type(err).__name__)
                    except Exception as e:
                        log.warning("callpilot_call_analysis_blocked error_type=%s",type(e).__name__)
        except Exception as e:
            log.warning("callpilot_call_intelligence_initial_sync blocked=%s",type(e).__name__)
    stripe_key=os.getenv("STRIPE_SECRET_KEY","")
    price_id=os.getenv("STRIPE_PRICE_ID","")
    if stripe_key.startswith("sk_test_") and price_id.startswith("price_"):
        try:
            with httpx.Client(timeout=7) as c:
                resp=c.get("https://api.stripe.com/v1/prices/"+price_id,auth=(stripe_key,""))
            valid=resp.status_code==200
            price=resp.json() if valid else {}
            log.info("callpilot_stripe_price_probe http_status=%s valid=%s active=%s recurring=%s livemode=%s",
                     resp.status_code,valid,bool(price.get("active")),
                     bool(price.get("recurring")),price.get("livemode") if valid else "unknown")
        except Exception as e:
            log.warning("callpilot_stripe_price_probe failed error_type=%s",type(e).__name__)
    if vapi_key:
        try:
            with httpx.Client(timeout=5) as c:
                r=c.get("https://api.vapi.ai/assistant",params={"limit":"1"},headers={"Authorization":"Bearer "+vapi_key})
            log.info("callpilot_vapi_probe http_status=%s authenticated=%s",r.status_code,r.status_code==200)
            # Verify the authenticated recording endpoint without downloading audio
            # or logging private presigned URLs.
            try:
                from app.cloud import api as cloud_api
                recorded=cloud_api("call_insights",params={
                    "recording_url":"not.is.null","select":"provider_call_id","limit":1})
                if recorded:
                    with httpx.Client(timeout=10,follow_redirects=False) as c:
                        response=c.get("https://api.vapi.ai/call/"+
                            recorded[0]["provider_call_id"]+"/mono-recording",
                            headers={"Authorization":"Bearer "+vapi_key})
                    log.info("callpilot_recording_auth_probe http_status=%s redirect_issued=%s",
                             response.status_code,response.status_code in (301,302,303,307,308))
            except Exception as exc:
                log.warning("callpilot_recording_auth_probe error_type=%s",type(exc).__name__)
        except Exception as e:
            log.warning("callpilot_vapi_probe failed error_type=%s",type(e).__name__)



from app.account import router as account_router
from app.billing import router as billing_router
app.include_router(account_router)
app.include_router(billing_router)

@app.get('/app',response_class=HTMLResponse)
def customer_app():
    return (ROOT/'static'/'customer.html').read_text()

@app.get('/legal',response_class=HTMLResponse)
def legal_page():
    return (ROOT/'static'/'legal.html').read_text()

@app.get('/operator',response_class=HTMLResponse)
def operator_app():
    return (ROOT/'static'/'cloud.html').read_text()

from app.call_intel import router as call_intel_router
app.include_router(call_intel_router)

from app.calendar import router as calendar_router
app.include_router(calendar_router)

from app.highlevel import router as highlevel_router
from app.sheets import router as sheets_router
app.include_router(sheets_router)
app.include_router(highlevel_router)

from app.agent_builder import router as agent_builder_router
app.include_router(agent_builder_router)

from app.analytics import router as analytics_router
app.include_router(analytics_router)

from app.identity import router as identity_router
app.include_router(identity_router)

from app.demo_links import router as demo_links_router
app.include_router(demo_links_router)

from app.transfer_directory import router as transfer_directory_router
app.include_router(transfer_directory_router)

from app.runtime_routing import router as runtime_routing_router
app.include_router(runtime_routing_router)
from app.phone_provisioning import router as phone_provisioning_router
app.include_router(phone_provisioning_router)

from app.shopify_connection import router as shopify_connection_router
app.include_router(shopify_connection_router)
from app.shopify_checkout import router as shopify_checkout_router
app.include_router(shopify_checkout_router)
from app.shopify_fulfillment import router as shopify_fulfillment_router
app.include_router(shopify_fulfillment_router)
from app.call_menu import router as call_menu_router
app.include_router(call_menu_router)
from app.worker_forms import router as worker_forms_router
app.include_router(worker_forms_router)
from app.booking_links import router as booking_links_router
app.include_router(booking_links_router)

from app.workers import router as workers_router
app.include_router(workers_router)

from app.booking_hours import router as booking_hours_router
app.include_router(booking_hours_router)

from app.voice_preview import router as voice_preview_router
app.include_router(voice_preview_router)

from app.team import router as team_router
app.include_router(team_router)

from app.catalog import router as catalog_router
app.include_router(catalog_router)

from app.usage import router as usage_router
app.include_router(usage_router)
