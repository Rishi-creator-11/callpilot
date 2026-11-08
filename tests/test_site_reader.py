"""The site reader: structured product data, safe discovery, clean policies and tolerance for broken pages."""
import json

import pytest
from fastapi import HTTPException

from app import site_reader as sr

PRODUCT = {
    "id": "1", "title": "Flyers", "slug": "flyers", "category": "Print Materials", "description": "Eye-catching flyers for events.",
    "base_price_text": "Starting at $0.00", "price": 49.99, "design_fee": 45,
    "options_schema": {"fields": [
        {"name": "Size", "type": "select", "label": "Size", "options": ["8.5x11", "4x6"]},
        {"name": "Paper", "type": "select", "label": "Paper", "options": ["Gloss", "Matte"]},
        {"name": "files", "type": "file", "label": "Upload", "options": ["x"]},
        {"name": "quantity", "type": "select", "label": "Quantity", "options": ["250", "500"]}]},
    "price_matrix": [{"options": {"Size": "8.5x11", "Paper": "Gloss"}, "quantity": 250, "price": 110},
                     {"options": {"Size": "4x6", "Paper": "Matte"}, "quantity": 250, "price": 42.49},
                     {"options": {}, "quantity": 500, "price": 160}],
}


def page_with(obj, extra=""):
    flight = 'x[["$","div",null,{"product":' + json.dumps(obj) + "}]]"
    return f"<html><head><title>T</title></head><body><p>{extra}</p><script>self.__next_f.push([1,{json.dumps(flight)}])</script></body></html>"


def test_product_records_are_read_from_embedded_page_data():
    documents, flight = sr.embedded_documents(page_with(PRODUCT))
    found = sr.product_objects(flight)
    assert [o["title"] for o in found] == ["Flyers"]
    product = sr.normalize_product(found[0], "https://shop.example.com/products/flyers")
    assert product["name"] == "Flyers" and product["category"] == "Print Materials" and product["from_price"] == "$42.49"
    assert "Size: 8.5x11, 4x6" in product["details"] and "Quantity: 250, 500" in product["details"] and "Upload" not in product["details"]
    assert "$" not in product["details"]
    assert "Starting at $42.49." in product["price_note"] and "250 × Size 4x6, Paper Matte: $42.49" in product["price_note"]
    assert "500: $160" in product["price_note"] and "Design help is $45." in product["price_note"]
    assert product["url"] == "https://shop.example.com/products/flyers"


def test_option_fields_and_empty_records_never_become_products():
    assert sr.normalize_product({"title": "quantity", "price": 3}, "u") is None
    assert sr.normalize_product({"title": "Mystery"}, "u") is None
    assert sr.normalize_product({"title": "X"}, "u") is None


def test_json_ld_products_and_listing_text_fallback():
    ld = [{"@context": "https://schema.org", "@graph": [{"@type": "Product", "name": "Mug", "description": "A mug.", "offers": {"price": "12.50"}}]}]
    assert [(p["name"], p["from_price"]) for p in sr.products_from_ld(ld, "https://x.com/")] == [("Mug", "$12.50")]
    text = "Flyers\nEye-catching flyers for promotions and events.\nFrom $42.49\nBanners\nIndoor and outdoor banners that last for years.\nFrom $75.00"
    assert [(p["name"], p["from_price"]) for p in sr.products_from_listing(text, "u")] == [("Flyers", "$42.49"), ("Banners", "$75")]


@pytest.mark.parametrize("path,kind", [("/", "home"), ("/contact", "key"), ("/refund-policy", "policy"), ("/privacy", "legal"),
                                       ("/products/shipping-boxes", "item"), ("/products", "index"), ("/business-cards/premium/x", "item")])
def test_page_kinds(path, kind):
    assert sr.page_kind(path) == kind


def test_candidate_urls_stay_on_site_skip_noise_and_put_key_pages_first():
    links = [("/cart", ""), ("/contact", ""), ("/products?category=Apparel", ""), ("https://evil.example.net/contact", ""), ("/logo.png", "")]
    sitemap = ["https://www.shop.example.com/products/flyers", "https://shop.example.com/about", "https://shop.example.com/account/orders"]
    urls = sr.candidate_urls("https://shop.example.com/", links, sitemap)
    assert urls == ["https://shop.example.com/about", "https://shop.example.com/contact", "https://www.shop.example.com/products/flyers"]


def test_read_site_combines_pages_tolerates_failures_and_dedupes(monkeypatch):
    home = ("<html><head><title>Metro</title><meta name='description' content='We print things.'></head><body>"
            "<a href='/contact'>Contact</a><a href='/products/flyers'>Flyers</a><a href='/refund-policy'>Refunds</a><a href='/products/broken'>B</a>"
            "<h1>Print that looks great</h1><p>Open Mon - Fri 9am to 5pm for walk-ins and orders.</p></body></html>")
    contact = "<html><head><title>Contact</title></head><body><p>Email info@shop.example.com or call (973) 555-0100 for help with any order.</p></body></html>"
    refund = "<html><head><title>Refunds</title></head><body><h1>Refunds</h1><p>" + "We refund orders that have not entered production yet. " * 4 + "</p></body></html>"
    pages = {"https://shop.example.com/": home, "https://shop.example.com/contact": contact, "https://shop.example.com/refund-policy": refund,
             "https://shop.example.com/products/flyers": page_with(PRODUCT, "Flyers page with plenty of readable text here.")}
    sitemap = "<urlset><url><loc>https://shop.example.com/products/flyers</loc></url><url><loc>https://shop.example.com/products/broken</loc></url></urlset>"

    def download(url, allow_xml=False):
        if url.endswith("sitemap.xml"):
            return url, "shop.example.com", "application/xml", sitemap
        if url not in pages:
            raise OSError("down")
        return url, "shop.example.com", "text/html", pages[url]

    monkeypatch.setattr(sr.wi, "_download", download)
    site = sr.read_site("https://shop.example.com/")
    assert site["description"] == "We print things." and site["email"] == "info@shop.example.com" and site["phone"] == "(973) 555-0100"
    assert [p["name"] for p in site["products"]] == ["Flyers"] and site["stats"]["products"] == 1
    assert [p["title"] for p in site["policies"]] == ["Refund Policy"] and any("Mon - Fri" in h for h in site["hours"])


def test_an_unreadable_first_page_is_reported(monkeypatch):
    def download(url, allow_xml=False):
        raise OSError("down")
    monkeypatch.setattr(sr.wi, "_download", download)
    with pytest.raises(HTTPException):
        sr.read_site("https://shop.example.com/")


def test_the_advertised_listing_price_wins_over_the_cheapest_matrix_row(monkeypatch):
    listing = "<html><head><title>Products</title></head><body><h1>All products</h1><p>Choose a product and see the exact price.</p><div>Flyers</div><div>Eye-catching flyers for events and promotions today.</div><div>From $42.49</div></body></html>"
    home = "<html><head><title>Metro</title></head><body><a href='/products'>Products</a><a href='/products/flyers'>Flyers</a><p>Welcome to our print shop, open daily.</p></body></html>"
    pages = {"https://shop.example.com/": home, "https://shop.example.com/products": listing,
             "https://shop.example.com/products/flyers": page_with(PRODUCT, "Flyers page with plenty of readable text here.")}

    def download(url, allow_xml=False):
        if url not in pages:
            raise OSError("missing")
        return url, "shop.example.com", "text/html", pages[url]

    monkeypatch.setattr(sr.wi, "_download", download)
    flyers = sr.read_site("https://shop.example.com/")["products"][0]
    assert flyers["from_price"] == "$42.49" and flyers["price_note"].startswith("Starting at $42.49.") and "$110" in flyers["price_note"]


def test_hostile_page_data_cannot_make_parsing_slow_or_crash():
    nested = "[" * 100000 + "]" * 100000
    html = f"<script type='application/ld+json'>{nested}</script><script>self.__next_f.push([1,{json.dumps(nested)}])</script>"
    documents, flight = sr.embedded_documents(html)
    assert documents == [] and isinstance(flight, str)
    junk = '{"id":"a",' * 50000 + '"options_schema":1' * 5000
    import time
    started = time.monotonic()
    assert sr.product_objects(junk) == []
    assert time.monotonic() - started < 6


def test_faqs_are_captured_with_answers_and_calls_to_action_are_skipped():
    text = ("Questions\nGood to know\nHow fast will my order be ready?\nMost print products move to production within one business day of artwork approval.\n"
            "Can you design my artwork?\nYes, a designer will contact you within 1 business day.\nThe flat fee is shown before you add to cart.\n"
            "Ready to start your project?\nSubmit a quote request and our team will follow up with you soon.\nShop online\n")
    faqs = sr.extract_faqs(text)
    assert [f["question"] for f in faqs] == ["How fast will my order be ready?", "Can you design my artwork?"]
    assert "one business day" in faqs[0]["answer"] and "flat fee" in faqs[1]["answer"] and "Shop online" not in faqs[1]["answer"]


def test_prices_keep_their_units_and_wording():
    products = [{"name": "Graphic Design Services", "from_price": "$75", "price_note": "Starting at $75."},
                {"name": "Branding", "from_price": "$499", "price_note": "Starting at $499."},
                {"name": "Flyers", "from_price": "$42.49", "price_note": "Starting at $42.49."}]
    sr.price_phrases("Graphic Design ServicesProfessional design.Starting at $75/hr\nBrandingComplete packages.Packages from $499\nFlyers Eye-catching.From $42.49", products)
    assert products[0]["from_price"] == "$75/hr" and products[0]["price_note"].startswith("Starting at $75/hr.")
    assert products[1]["from_price"] == "$499" and products[1]["price_note"].startswith("Packages from $499.")
    assert products[2]["from_price"] == "$42.49" and products[2]["price_note"] == "Starting at $42.49."


def test_inline_elements_no_longer_glue_words_together():
    page = sr.wi.PageText()
    page.feed("<html><head><title>T</title></head><body><a href='/x'><span>Business Cards</span><span>Standard, premium cards</span><span>12 products</span></a></body></html>")
    page.close()
    assert page.result("x.com")[1] == "Business Cards Standard, premium cards 12 products"


def test_accordion_question_buttons_are_kept():
    page = sr.wi.PageText()
    page.feed("<html><head><title>T</title></head><body><button>How fast will my order be ready?</button><p>Within one business day of approval.</p></body></html>")
    page.close()
    assert "How fast will my order be ready?" in page.result("x.com")[1]
