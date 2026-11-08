"""Read a whole business website into clear, checkable knowledge.

The reader crawls the sitemap and key pages in parallel (through the same SSRF-safe downloader as the
single-page import), cleans the visible text, and also reads the structured data many sites embed:
JSON-LD, Next.js page data, and product records with their options and price matrices. Products and
prices come from that structured data when it exists, so names, sizes and prices are exact, never guessed.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit, urlunsplit

from app import website_import as wi

MAX_PAGES = 90
BUDGET_SECONDS = 40
WORKERS = 6
MAX_PRODUCTS = 120
SKIP_PATH = re.compile(r"^/(account|cart|checkout|login|signin|sign-in|register|admin|api|_next|static|wp-admin)(/|$)", re.I)
FILE_PATH = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|zip|mp4|mov|css|js|ico|xml|json|txt)$", re.I)
POLICY_PATH = re.compile(r"refund|reprint|return|shipping-policy|delivery|policy|policies|warranty|cancel", re.I)
LEGAL_PATH = re.compile(r"privacy|terms|cookie", re.I)
OPTION_NAMES = {"quantity", "size", "paper", "type", "color", "colour", "material", "finish", "shape", "corners", "texture", "style"}
KEY_PATH = re.compile(r"contact|about|faq|hours|location|visit|team|how-it-works|request-quote|quote|services|pricing", re.I)
PRICE = re.compile(r"\$\s*\d[\d,]*(?:\.\d{1,2})?")
DAY = r"(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*"
HOURS_LINE = re.compile(r"\b" + DAY + r"\b\s*(?:-|–|—|to|through|thru)\s*\b" + DAY + r"\b.{0,40}?\d", re.I)
PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def money(value) -> str:
    amount = float(value)
    return "$" + (f"{amount:,.0f}" if amount == int(amount) else f"{amount:,.2f}")


def clean(text, limit=None) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit] if limit else text


# ------------------------------------------------------------------ embedded structured data
_FLIGHT = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_LD = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_ANCHORS = ('"options_schema"', '"price_matrix"', '"base_price_text"')
_STARTS = ('{"id":', '{"title":', '{"name":', '{"slug":')


def embedded_documents(html: str) -> tuple[list, str]:
    """JSON-LD documents, plus the decoded Next.js page data as one string."""
    documents = []
    for raw in _LD.findall(html)[:20]:
        try:
            documents.append(json.loads(raw[:200_000]))
        except (ValueError, RecursionError):
            continue
    flight = ""
    pieces = []
    for chunk in _FLIGHT.findall(html)[:400]:
        try:
            pieces.append(json.loads(chunk))
        except (ValueError, RecursionError):
            continue
    flight = "".join(pieces)[:2_000_000]
    next_data = _NEXT_DATA.search(html)
    if next_data:
        flight += next_data.group(1)
    return documents, flight


def product_objects(flight: str) -> list[dict]:
    """Find product records in page data by anchoring on keys only a product carries."""
    decoder = json.JSONDecoder()
    found, seen = [], set()
    hits, started = 0, time.monotonic()
    for anchor in _ANCHORS:
        key_name = anchor.strip('"')
        position = 0
        while True:
            at = flight.find(anchor, position)
            hits += 1
            if at < 0 or hits > 300 or time.monotonic() - started > 3:
                break
            position = at + len(anchor)
            end = at
            for _ in range(40):  # walk back through candidate starts until one decodes to an object that owns this key
                start = max((flight.rfind(marker, max(0, at - 8000), end) for marker in _STARTS), default=-1)
                if start < 0:
                    break
                end = start
                try:
                    obj, _ = decoder.raw_decode(flight[start:start + 60_000])
                except (ValueError, RecursionError):
                    continue
                if isinstance(obj, dict) and key_name in obj and (obj.get("title") or obj.get("name")):
                    identity = str(obj.get("slug") or obj.get("title") or obj.get("name")).lower()
                    if identity not in seen:
                        seen.add(identity)
                        found.append(obj)
                    break
    return found


def option_fields(obj: dict) -> list[dict]:
    schema = obj.get("options_schema")
    fields = (schema or {}).get("fields") if isinstance(schema, dict) else None
    result = []
    for field in fields or []:
        if not isinstance(field, dict) or str(field.get("type", "")).lower() in {"file", "upload", "files"}:
            continue
        values = [clean(v) for v in (field.get("options") or []) if clean(v)]
        label = clean(field.get("label") or field.get("name"))
        if label and values:
            result.append({"label": label, "values": values[:14] + (["and more"] if len(values) > 14 else [])})
    return result


def normalize_product(obj: dict, page_url: str) -> dict | None:
    name = clean(obj.get("title") or obj.get("name"), 100)
    if len(name) < 2 or name.casefold() in OPTION_NAMES:
        return None
    description = clean(obj.get("description") or obj.get("seo_description"), 400)
    category = clean(obj.get("category"), 60)
    options = option_fields(obj)
    rows = [r for r in (obj.get("price_matrix") or []) if isinstance(r, dict) and isinstance(r.get("price"), (int, float)) and r["price"] > 0]
    from_price = min((r["price"] for r in rows), default=None)
    if from_price is None:
        for key in ("price", "base_price", "from_price"):
            if isinstance(obj.get(key), (int, float)) and obj[key] > 0:
                from_price = obj[key]
                break
    examples = []
    for row in sorted(rows, key=lambda r: (r.get("quantity") or 0, r["price"]))[:200]:
        label = ", ".join(f"{k} {v}" for k, v in (row.get("options") or {}).items())
        quantity = f"{row.get('quantity')}" if row.get("quantity") else ""
        subject = " × ".join(part for part in (quantity, label) if part) or "Base price"
        entry = f"{subject}: {money(row['price'])}"
        if entry not in examples:
            examples.append(entry)
    parts = [f"{name}" + (f" ({category})" if category else "") + "."]
    if description:
        parts.append(description)
    if options:
        parts.append("Options — " + "; ".join(f"{o['label']}: {', '.join(o['values'])}" for o in options) + ".")
    details = " ".join(parts)
    price_bits = []
    if from_price:
        price_bits.append(f"Starting at {money(from_price)}.")
    if examples:
        price_bits.append("Examples: " + "; ".join(examples[:6]) + ". The exact price depends on the options chosen.")
    fee = obj.get("design_fee")
    if isinstance(fee, (int, float)) and fee > 0:
        price_bits.append(f"Design help is {money(fee)}.")
    slug = clean(obj.get("slug"))
    if not (description or options or from_price):
        return None
    return {"name": name, "category": category, "description": description, "options": options,
            "from_price": money(from_price) if from_price else "", "details": details[:1400],
            "price_note": " ".join(price_bits)[:900],
            "url": urljoin(page_url, "/products/" + slug) if slug else page_url}


def products_from_ld(documents: list, page_url: str) -> list[dict]:
    products = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "Product" in kinds and node.get("name"):
                offers = node.get("offers")
                offers = offers[0] if isinstance(offers, list) and offers else offers
                price = None
                if isinstance(offers, dict):
                    for key in ("price", "lowPrice"):
                        try:
                            price = float(offers.get(key))
                            break
                        except (TypeError, ValueError):
                            continue
                item = normalize_product({"title": node["name"], "description": node.get("description"),
                                          "category": node.get("category"), "price": price}, page_url)
                if item:
                    products.append(item)
            for value in node.values():
                if isinstance(value, (list, dict)):
                    walk(value)

    walk(documents)
    return products


def products_from_listing(text: str, page_url: str) -> list[dict]:
    """Fallback for plain pages: a title line, a description line, then a 'From $X' line."""
    lines = [clean(line) for line in text.splitlines() if clean(line)]
    products = []
    for i, line in enumerate(lines):
        match = re.fullmatch(r"(?:from|starting at|starts at)\s*(\$\s*\d[\d,]*(?:\.\d{1,2})?)(?:\s+\$\s*\d[\d,]*(?:\.\d{1,2})?)?", line, re.I)
        if not match or i < 1:
            continue
        description = lines[i - 1] if i >= 2 and len(lines[i - 1]) > 30 else ""
        title = lines[i - 2] if description and i >= 2 else lines[i - 1]
        if len(title) < 3 or len(title) > 90 or PRICE.search(title):
            continue
        amount = float(re.sub(r"[^\d.]", "", match.group(1)))
        products.append({"name": title, "category": "", "description": description, "options": [], "from_price": money(amount),
                         "details": clean(title + ". " + description, 600), "price_note": f"Starting at {money(amount)}.", "url": page_url})
    return products


def advertised_prices(text: str) -> tuple[dict, dict]:
    """Prices shown next to each card on a listing page, keyed by the card's description and by its title when present."""
    lines = [clean(line) for line in text.splitlines() if clean(line)]
    by_description, by_name = {}, {}
    for i, line in enumerate(lines):
        match = re.fullmatch(r"(?:from|starting at|starts at)\s*(\$\s*\d[\d,]*(?:\.\d{1,2})?)(?:\s+\$\s*\d[\d,]*(?:\.\d{1,2})?)?", line, re.I)
        if not match or i < 1:
            continue
        price = money(float(re.sub(r"[^\d.]", "", match.group(1))))
        by_description.setdefault(lines[i - 1].casefold(), price)
        if i >= 2:
            by_name.setdefault(lines[i - 2].casefold(), price)
    return by_description, by_name


# ------------------------------------------------------------------ discovery and crawling
def page_kind(path: str) -> str:
    if path in ("", "/"):
        return "home"
    if LEGAL_PATH.search(path):
        return "legal"
    if path.startswith("/products/") or path.count("/") >= 3:
        return "item"
    if POLICY_PATH.search(path):
        return "policy"
    if KEY_PATH.search(path):
        return "key"
    if path.rstrip("/") == "/products" or re.fullmatch(r"/[^/]+", path):
        return "index"
    return "item"


def candidate_urls(home_url: str, links: list[tuple[str, str]], sitemap_urls: list[str]) -> list[str]:
    base = urlsplit(home_url)
    host = base.hostname.removeprefix("www.")
    seen, scored = {base.path.rstrip("/") or "/"}, []
    for raw in [href for href, _ in links] + sitemap_urls:
        try:
            target = urlsplit(urljoin(home_url, raw))
        except ValueError:
            continue
        if target.scheme not in ("http", "https") or (target.hostname or "").removeprefix("www.") != host:
            continue
        path = target.path or "/"
        if target.query or SKIP_PATH.search(path) or FILE_PATH.search(path):
            continue
        key = path.rstrip("/") or "/"
        if key in seen:
            continue
        seen.add(key)
        rank = {"home": 0, "key": 1, "policy": 1, "index": 2, "item": 3, "legal": 4}[page_kind(path)]
        scored.append((rank, len(path), urlunsplit((target.scheme, target.netloc, path, "", ""))))
    return [url for _, _, url in sorted(scored)][:MAX_PAGES]


def sitemap_urls(home_url: str, deadline: float) -> list[str]:
    try:
        _, _, _, body = wi._download(urljoin(home_url, "/sitemap.xml"), allow_xml=True)
    except Exception:  # noqa: BLE001 - a missing sitemap is normal
        return []
    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
    children = [u for u in urls if u.endswith(".xml")][:3]
    urls = [u for u in urls if not u.endswith(".xml")]
    for child in children:
        if time.monotonic() > deadline:
            break
        try:
            _, _, _, more = wi._download(child, allow_xml=True)
            urls += [u for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", more) if not u.endswith(".xml")]
        except Exception:  # noqa: BLE001
            continue
    return urls


def meta_description(html: str) -> str:
    for pattern in (r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']',
                    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']description["\']',
                    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)["\']'):
        match = re.search(pattern, html, re.I)
        if match:
            import html as html_lib
            return clean(html_lib.unescape(match.group(1)), 400)
    return ""


def read_one(url: str) -> dict | None:
    try:
        current, host, media_type, body = wi._download(url)
    except Exception:  # noqa: BLE001 - one broken page never stops the crawl
        return None
    path = urlsplit(current).path
    text, title, links = "", host, []
    try:
        page, parsed = wi.parse_page(current, host, media_type, body)
        text, title, links = parsed["content"], parsed["title"], page.links
    except Exception:  # noqa: BLE001 - a page with no readable text can still hold structured data
        pass
    documents, flight = embedded_documents(body) if media_type != "text/plain" else ([], "")
    products = [p for p in (normalize_product(o, current) for o in product_objects(flight)) if p]
    products += products_from_ld(documents, current)
    return {"url": current, "path": path, "title": title, "text": text, "links": links, "kind": page_kind(path),
            "description": meta_description(body), "products": products}


def read_site(raw_url: str, *, max_pages: int = MAX_PAGES, budget: float = BUDGET_SECONDS) -> dict:
    """Read a website. Raises HTTPException (from the safe downloader) only if the first page is unreadable."""
    started = time.monotonic()
    deadline = started + budget
    home = wi.normalize_url(raw_url)
    first = read_one(home)
    if first is None:
        raise wi.HTTPException(422, "Could not read this website. Check the address or tell us about your business instead.")
    home_url = first["url"]
    urls = candidate_urls(home_url, first["links"], sitemap_urls(home_url, deadline))[:max_pages]
    pages = [first]

    def work(url):
        return read_one(url) if time.monotonic() < deadline else None

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        pages += [page for page in pool.map(work, urls) if page]

    products, names = [], {}
    for page in pages:
        for product in page["products"]:
            key = product["name"].casefold()
            if key in names:
                names[key].update({k: v for k, v in product.items() if v and not names[key].get(k)})
                continue
            names[key] = product
            products.append(product)
    # The price a business advertises on its own listing pages is the one the agent should quote.
    by_description, by_name = {}, {}
    for page in pages:
        if page["kind"] in ("index", "home", "key"):
            more_description, more_name = advertised_prices(page["text"])
            for key, price in more_description.items():
                by_description.setdefault(key, price)
            for key, price in more_name.items():
                by_name.setdefault(key, price)
            if len(products) < 3:  # Sites with no structured data: the listing is all there is.
                for listed in products_from_listing(page["text"], page["url"]):
                    if listed["name"].casefold() not in names:
                        names[listed["name"].casefold()] = listed
                        products.append(listed)
    for product in products:
        price = (by_description.get(product["description"].casefold()) if product["description"] else None) or by_name.get(product["name"].casefold())
        if price and product["from_price"] != price:
            note = re.sub(r"^Starting at \$[\d,]+(?:\.\d{1,2})?\.", "", product["price_note"]).strip()
            product["from_price"] = price
            product["price_note"] = (f"Starting at {price}. " + note).strip()
    texts = "\n".join(page["text"] for page in pages if page["kind"] in ("home", "key"))
    contact_text = "\n".join(page["text"] for page in pages if "contact" in page["path"].lower()) or texts
    link_blob = " ".join(href for page in pages[:3] for href, _ in page["links"])
    emails = list(dict.fromkeys(EMAIL.findall(contact_text + " " + link_blob)))
    phones = list(dict.fromkeys(m.group(0) for m in PHONE.finditer(contact_text)))
    hours = list(dict.fromkeys(clean(line, 160) for line in texts.splitlines() if HOURS_LINE.search(line)))[:4]
    policies = [{"title": re.sub(r"[-_]+", " ", page["path"].strip("/").split("/")[-1]).title(), "url": page["url"],
                 "text": clean(page["text"].split("\n", 2)[-1] if page["text"].count("\n") >= 2 else page["text"], 1800)}
                for page in pages if page["kind"] == "policy" and len(page["text"]) > 120]
    return {
        "source_url": home_url, "title": first["title"], "description": first["description"],
        "pages": [{"url": p["url"], "title": p["title"], "kind": p["kind"], "chars": len(p["text"])} for p in pages],
        "text": {kind: "\n".join(f"{p['title']}\n{p['text']}" for p in pages if p["kind"] == kind) for kind in ("home", "key", "policy")},
        "products": products[:MAX_PRODUCTS], "email": emails[0] if emails else "", "phone": phones[0] if phones else "",
        "hours": hours, "policies": policies[:8],
        "stats": {"pages_read": len(pages), "products": len(products), "seconds": round(time.monotonic() - started, 1)},
    }
