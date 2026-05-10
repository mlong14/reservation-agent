import json
import logging
import os
import time
import anthropic
import requests as http_requests

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


def discover_restaurants(city: str = "San Francisco", count: int = 5) -> list[str]:
    """Use Claude with web search to discover new interesting restaurants in the given city."""
    client = anthropic.Anthropic()

    prompt = (
        f"Search for the best new restaurants in {city} that are worth visiting for a special dinner. "
        f"Focus on places that opened or became notable recently, have interesting cuisine, "
        f"and are bookable on Resy or OpenTable. "
        f"Return ONLY a JSON array of {count} restaurant names, no other text. Example: "
        f'["Restaurant A", "Restaurant B", "Restaurant C"]'
    )

    try:
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=1024,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )

        for block in response.content:
            if hasattr(block, "text"):
                text = block.text.strip()
                start = text.find("[")
                end = text.rfind("]") + 1
                if start >= 0 and end > start:
                    try:
                        return json.loads(text[start:end])
                    except json.JSONDecodeError:
                        pass

        logging.warning("Could not parse restaurant list from Claude discovery response.")
        return []

    except Exception as e:
        logging.error(f"Restaurant discovery error: {e}")
        return []


def get_place_rating(api_key: str, restaurant_name: str, retries: int = 3) -> dict | None:
    """Look up a specific restaurant's Google rating by name in SF.

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
                wait = 30 * (attempt + 1)  # 30s, 60s, 90s
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


def discover_via_google_places(
    api_key: str,
    min_rating: float = 4.3,
    min_rating_count: int = 100,
    max_pages: int = 5,
) -> tuple[list[dict], int]:
    """Search Google Places (New) Text Search for highly-rated SF restaurants.

    Uses the Advanced SKU (includes rating field in results).
    Each page costs ~1 API call and returns up to 20 places.

    Returns (places, api_calls_made) where each place is:
      {name, place_id, rating, rating_count, address}
    """
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.id,places.displayName,places.rating,places.userRatingCount,places.formattedAddress,nextPageToken",
    }

    results = []
    page_token = None
    calls_made = 0

    for _ in range(max_pages):
        body = {
            "textQuery": "restaurants in San Francisco CA",
            "minRating": min_rating,
            "pageSize": 20,
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": 37.7749, "longitude": -122.4194},
                    "radius": 15000.0,
                }
            },
        }
        if page_token:
            body["pageToken"] = page_token

        try:
            resp = http_requests.post(url, json=body, headers=headers, timeout=15)
            resp.raise_for_status()
            calls_made += 1
            data = resp.json()
        except Exception as e:
            logging.error(f"Google Places API error: {e}")
            break

        for place in data.get("places", []):
            rating = place.get("rating", 0)
            count = place.get("userRatingCount", 0)
            name = place.get("displayName", {}).get("text", "")
            if name and count >= min_rating_count:
                results.append({
                    "name": name,
                    "place_id": place.get("id", ""),
                    "rating": rating,
                    "rating_count": count,
                    "address": place.get("formattedAddress", ""),
                })

        page_token = data.get("nextPageToken")
        if not page_token:
            break

        time.sleep(0.5)

    logging.info(f"Google Places: found {len(results)} restaurants in {calls_made} API calls")
    return results, calls_made


def parse_google_takeout_saved_places(file_path: str) -> list[str]:
    """Parse a Google Maps Takeout 'Saved Places.json' and return place names.

    To get this file: Google Takeout → select Maps (Your places) → export.
    Set up recurring exports every 2 months to Google Drive, then download here.
    """
    if not os.path.exists(file_path):
        logging.warning(f"Takeout file not found: {file_path}")
        return []

    try:
        with open(file_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logging.error(f"Could not read Takeout file: {e}")
        return []

    names = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        name = props.get("Title") or props.get("name")
        if name:
            names.append(name)

    logging.info(f"Found {len(names)} saved places in Takeout export.")
    return names
