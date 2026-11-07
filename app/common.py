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

def wrapup_hook() -> dict:
    return {"name": WRAPUP_HOOK_NAME, "on": "call.timeElapsed",
            "options": {"seconds": WRAPUP_AT_SECONDS},
            "do": [{"type": "say", "exact": "We're almost at the end of this call. Is there one last thing I can help with?"}]}

PRICE = re.compile(r"\b(price|prices|pricing|cost|costs|fee|fees|quote|estimate|charge|charges|rate|rates|discount|how much|ballpark|dollars?|usd)\b|\$", re.I)
MONEY = re.compile(r"\$\s*\d|\b\d+(?:\.\d+)?\s*(?:dollars?|usd|bucks)\b", re.I)
NO_PRICE = "I can't quote prices or estimates. I can arrange for a team member to follow up."
UNKNOWN = "I don't have verified information about that. I can ask a team member to follow up."
