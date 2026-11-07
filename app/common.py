"""Shared helpers and launch policy constants. No router wiring, so any module may import it."""
from __future__ import annotations
import os, hmac, re
from fastapi import Header, HTTPException

RENDER_BASE = "https://callpilot-internal.onrender.com"
MAX_CALL_SECONDS = 240
WRAPUP_AT_SECONDS = 215
WRAPUP_HOOK_NAME = "callpilot-four-minute-wrapup"

def authorize(x_admin_token: str | None = Header(default=None)):
    expected = os.getenv("ADMIN_TOKEN", "")
    if not expected or not x_admin_token or not hmac.compare_digest(expected, x_admin_token):
        raise HTTPException(401, "Admin token required")

def public_base() -> str:
    return os.getenv("PUBLIC_BASE_URL", RENDER_BASE).rstrip("/")

OPENING_PAUSE_SECONDS = 1
OPENING_HOOK_NAME = "callpilot-one-second-opening"

def opening_hook(greeting: str) -> dict:
    """Speak the existing greeting once, one second after connection.

    The assistant must use assistant-waits-for-user so the normal first message
    is not spoken immediately as well. Do not change the actual greeting.
    """
    return {"name": OPENING_HOOK_NAME, "on": "call.timeElapsed",
            "options": {"seconds": OPENING_PAUSE_SECONDS},
            "do": [{"type": "say", "exact": greeting}]}

def wrapup_hook() -> dict:
    return {"name": WRAPUP_HOOK_NAME, "on": "call.timeElapsed",
            "options": {"seconds": WRAPUP_AT_SECONDS},
            "do": [{"type": "say", "exact": "We're almost at the end of this call. Is there one last thing I can help with?"}]}

PRICE = re.compile(r"\b(price|prices|pricing|cost|costs|fee|fees|quote|estimate|charge|charges|rate|rates|discount|how much|ballpark|dollars?|usd|precio|precios|cuánto|cuanto|cuesta|cotización|cotizacion|tarifa|costo|costos|presupuesto)\b|\$", re.I)
MONEY = re.compile(r"\$\s*\d|\b\d+(?:\.\d+)?\s*(?:dollars?|usd|bucks)\b", re.I)
NO_PRICE = "I can help with the product options and collect details for an accurate quote, but I don't have a verified price for that exact configuration."
UNKNOWN = "I don't have verified information about that. I can ask a team member to follow up."
