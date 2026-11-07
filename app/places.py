"""Server-side Google Maps business discovery for workspace setup."""

import os
from collections import deque
from threading import Lock
from time import monotonic

import httpx
from fastapi import HTTPException

_searches = deque()
_search_lock = Lock()


def configured() -> bool:
    return bool(os.getenv("GOOGLE_PLACES_API_KEY", "").strip())


def search_businesses(query: str, user_id: str) -> list[dict]:
    key = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    if not key:
        raise HTTPException(503, "Business search is unavailable. Enter your details manually.")
    with _search_lock:
        now = monotonic()
        while _searches and _searches[0][0] <= now - 60:
            _searches.popleft()
        if len(_searches) >= 30 or sum(uid == user_id for _, uid in _searches) >= 5:
            raise HTTPException(429, "Please wait a minute before searching again.")
        _searches.append((now, user_id))

    try:
        response = httpx.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers={
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask": "places.id,places.displayName.text,places.formattedAddress,places.attributions",
            },
            json={"textQuery": query, "pageSize": 5, "includePureServiceAreaBusinesses": True},
            timeout=8.0,
        )
        response.raise_for_status()
        places = response.json().get("places", [])
    except (httpx.HTTPError, ValueError, AttributeError):
        raise HTTPException(502, "Google Maps search is temporarily unavailable. Enter your details manually.") from None
    return [
        {
            "id": place["id"],
            "name": place["displayName"]["text"],
            "address": place.get("formattedAddress"),
            "attributions": place.get("attributions", []),
        }
        for place in places
        if place.get("id") and place.get("displayName", {}).get("text")
    ]
