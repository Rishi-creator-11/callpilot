"""Turn pasted menu text into one clear answer per dish and per section.

A menu usually reads: dish name, a line of ingredients, then a bare price. Voice agents answer best from short,
exact answers, so each dish becomes "The crab cake is $27" and each section becomes a short spoken list.
Nothing is invented: a price is only used when it appears in the text.
"""
from __future__ import annotations

import re

PRICE = re.compile(r"^\$?\s*(\d{1,3}(?:\.\d{2})?)$")
MARKET = re.compile(r"^(?:(.*?)\s+)?(MP|Market Price)$", re.I)


def money(value: str) -> str:
    return "$" + value


def parse(text: str) -> list[dict]:
    """Return sections: [{"name", "items": [{"name", "description", "price"}]}]. Price is '$27' or 'market price'."""
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    sections, current, pending = [], None, []
    for line in lines:
        price = PRICE.match(line)
        market = None if price else MARKET.match(line)
        if price or market:
            if market:
                name, cost = (market.group(1) or "").strip(), "market price"
                if name:
                    pending.append(name)
            else:
                cost = money(price.group(1))
            if not pending:
                continue
            # Name first, then ingredients; with a single line that line is the name. Extra leading lines are a section title.
            if len(pending) >= 3:
                title, pending = pending[:-2], pending[-2:]
                current = {"name": " ".join(title), "items": []}
                sections.append(current)
            name, description = (pending[0], pending[1]) if len(pending) == 2 else (pending[0], "")
            if current is None:
                current = {"name": "Menu", "items": []}
                sections.append(current)
            current["items"].append({"name": name, "description": description, "price": cost})
            pending = []
        else:
            pending.append(line)
    return [section for section in sections if section["items"]]


def spoken(price: str) -> str:
    return price if price == "market price" else price


def answers(menu_title: str, sections: list[dict]) -> list[tuple[str, str]]:
    """Question and answer pairs ready to save as FAQs."""
    out = []
    for section in sections:
        names = ", ".join("%s %s" % (item["name"], item["price"]) for item in section["items"])
        out.append(("What %s do you have on the %s?" % (section["name"].lower(), menu_title.lower()),
                    "On the %s, under %s we have: %s." % (menu_title.lower(), section["name"], names)))
        for item in section["items"]:
            cost = "market price, so ask the team for today's" if item["price"] == "market price" else item["price"]
            detail = (" It comes with " + item["description"].rstrip(".") + ".") if item["description"] else ""
            out.append(("How much is the %s?" % item["name"].lower(), "The %s is %s.%s" % (item["name"], cost, detail)))
    return out
