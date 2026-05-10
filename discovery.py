import logging
import time
import requests as http_requests


def get_place_rating(api_key: str, restaurant_name: str, retries: int = 3) -> dict | None:
    """Look up a specific restaurant's Google Places rating by name in SF.

    Returns {place_id, rating, rating_count} or None if not found.
    Costs 1 Places API call (Advanced SKU for rating field).
    Retries with backoff on rate-limit errors (429).
    """
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.id,places.rating,places.userRatingCount",
    }
    body = {
        "textQuery": f"{restaurant_name} San Francisco",
        "pageSize": 1,
        "locationBias": {
            "circle": {
                "center": {"latitude": 37.7749, "longitude": -122.4194},
                "radius": 15000.0,
            }
        },
    }
    for attempt in range(retries):
        try:
            resp = http_requests.post(url, json=body, headers=headers, timeout=10)
            if resp.status_code == 429:
                wait = 30 * (attempt + 1)
                logging.warning(f"Places API rate limit — waiting {wait}s (attempt {attempt + 1}/{retries})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            places = resp.json().get("places", [])
            if not places:
                return None
            p = places[0]
            return {
                "place_id": p.get("id"),
                "rating": p.get("rating", 0),
                "rating_count": p.get("userRatingCount", 0),
            }
        except Exception as e:
            logging.error(f"Google Places rating lookup failed for '{restaurant_name}': {e}")
            if attempt < retries - 1:
                time.sleep(10)
    return None
