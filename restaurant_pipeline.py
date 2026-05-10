"""
Restaurant discovery pipeline — independent phases.

  sync_resy    — Browse all SF venues on Resy → store in "Venue Data" tab
  sync_places  — Fetch Google Places ratings for venues missing them → store back
  sync_classify— Haiku batch-classifies venues as dinner date spots → store back
  rebuild      — Apply config filters to Venue Data → regenerate Restaurants tab

Running all:    .venv/bin/python restaurant_pipeline.py
Individual:     .venv/bin/python restaurant_pipeline.py --sync-resy
                .venv/bin/python restaurant_pipeline.py --sync-places
                .venv/bin/python restaurant_pipeline.py --sync-classify
                .venv/bin/python restaurant_pipeline.py --rebuild
Bot command:    /pipeline (runs all phases)

Changing filters in config.json only requires --rebuild (no API calls).
"""

import argparse
import json
import logging
import os
import time
import datetime

import anthropic
import requests as http_requests

import google_services
from booking_platforms import resy_client
from discovery import get_place_rating
from state import AgentState

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

VENUE_DATA_TAB = "Venue Data"
VENUE_DATA_HEADER = [
    "Name", "Resy ID", "URL Slug",
    "Google Rating", "Google Reviews", "Google Place ID",
    "Resy Fetched", "Date Spot",
]
# Columns: A=Name, B=Resy ID, C=URL Slug, D=Google Rating, E=Google Reviews,
#          F=Google Place ID, G=Resy Fetched, H=Date Spot


# ---------------------------------------------------------------------------
# Venue Data tab helpers
# ---------------------------------------------------------------------------

def _ensure_venue_data_tab(sheets_service, sheet_id: str):
    meta = sheets_service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = [s["properties"]["title"] for s in meta["sheets"]]
    if VENUE_DATA_TAB not in existing:
        logging.info("Creating Venue Data tab...")
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": VENUE_DATA_TAB}}}]},
        ).execute()

    # Always write the header row so new columns (e.g. Date Spot) are labelled
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{VENUE_DATA_TAB}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [VENUE_DATA_HEADER]},
    ).execute()


def _load_venue_data(sheets_service, sheet_id: str) -> dict[str, dict]:
    """Load Venue Data tab. Returns {resy_id: {row_index, name, g_rating, ..., date_spot}}"""
    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{VENUE_DATA_TAB}!A:H"
    ).execute()
    rows = result.get("values", [])
    if len(rows) < 2:
        return {}
    data = {}
    for i, row in enumerate(rows[1:], start=2):
        padded = row + [""] * (8 - len(row))
        resy_id = padded[1].strip()
        if resy_id:
            data[resy_id] = {
                "row_index": i,
                "name": padded[0],
                "url_slug": padded[2],
                "g_rating": padded[3],
                "g_reviews": padded[4],
                "g_place_id": padded[5],
                "resy_date": padded[6],
                "date_spot": padded[7],  # yes / no / unknown / ""
            }
    return data


def _append_venue_rows(sheets_service, sheet_id: str, rows: list[list]):
    if not rows:
        return
    sheets_service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{VENUE_DATA_TAB}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def _update_places_row(sheets_service, sheet_id: str, row_index: int, rating: float, reviews: int, place_id: str):
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{VENUE_DATA_TAB}!D{row_index}:F{row_index}",
        valueInputOption="USER_ENTERED",
        body={"values": [[rating, reviews, place_id]]},
    ).execute()


def _update_date_spot_column(sheets_service, sheet_id: str, updates: list[tuple[int, str]]):
    """Batch-update the Date Spot column (H) for multiple rows."""
    data = [{"range": f"{VENUE_DATA_TAB}!H{row}", "values": [[val]]} for row, val in updates]
    if data:
        sheets_service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()


def _get_restaurants_tab(config: dict) -> str:
    return config.get("google", {}).get("restaurants_tab", "Restaurants")


# ---------------------------------------------------------------------------
# Phase 1: Sync Resy
# ---------------------------------------------------------------------------

def sync_resy(config: dict, sheets_service) -> dict:
    """Browse all SF Resy venues, add new ones, and refresh Resy Fetched date for all current ones.

    Refreshing the date for existing venues lets rebuild detect stale entries
    (venues no longer on Resy) by checking how recently they were last seen.
    """
    sheet_id = config["google"]["sheet_id"]
    _ensure_venue_data_tab(sheets_service, sheet_id)

    existing = _load_venue_data(sheets_service, sheet_id)
    logging.info(f"Venue Data: {len(existing)} existing venues")

    all_venues = resy_client.browse_sf_venues(config["resy"])
    current_ids = {v["venue_id"] for v in all_venues}
    today = datetime.date.today().isoformat()

    # Append new venues
    new_rows = [
        [v["name"], v["venue_id"], v.get("url_slug", ""), "", "", "", today, ""]
        for v in all_venues
        if v["venue_id"] not in existing
    ]
    _append_venue_rows(sheets_service, sheet_id, new_rows)

    # Refresh Resy Fetched date for all venues still on Resy
    date_updates = [
        {"range": f"{VENUE_DATA_TAB}!G{v['row_index']}", "values": [[today]]}
        for vid, v in existing.items()
        if vid in current_ids
    ]
    if date_updates:
        sheets_service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": date_updates},
        ).execute()

    dropped = len(existing) - len(current_ids & existing.keys())
    logging.info(f"sync_resy: {len(all_venues)} on Resy, {len(new_rows)} new, {dropped} no longer listed")
    return {"total_resy": len(all_venues), "new_resy": len(new_rows), "dropped_resy": dropped}


# ---------------------------------------------------------------------------
# Phase 2: Sync Places (optional — skip if rate-limited)
# ---------------------------------------------------------------------------

def sync_places(config: dict, sheets_service) -> dict:
    """Fetch Google Places ratings for venues missing them in Venue Data."""
    sheet_id = config["google"]["sheet_id"]
    places_config = config.get("google_places", {})
    places_api_key = places_config.get("api_key")
    monthly_call_limit = places_config.get("monthly_call_limit", 900)

    if not places_api_key:
        logging.warning("google_places.api_key not set — skipping Places sync.")
        return {"places_updated": 0, "places_not_found": 0}

    state = AgentState()
    monthly_calls = state.get_monthly_places_api_calls()

    venue_data = _load_venue_data(sheets_service, sheet_id)
    needs_places = [(rid, v) for rid, v in venue_data.items() if not v["g_rating"]]
    logging.info(f"sync_places: {len(needs_places)} venues need Places data ({monthly_calls}/{monthly_call_limit} used this month)")

    today = datetime.date.today().isoformat()
    updated = 0
    not_found = 0

    for resy_id, v in needs_places:
        if monthly_calls >= monthly_call_limit:
            logging.warning("Monthly Places API limit reached — stopping.")
            break

        place = get_place_rating(places_api_key, v["name"])
        monthly_calls += 1
        state.record_places_api_calls(1)
        time.sleep(2.0)

        if place:
            _update_places_row(
                sheets_service, sheet_id, v["row_index"],
                place["rating"], place["rating_count"], place.get("place_id", ""),
            )
            logging.info(f"  {v['name']}: {place['rating']}★ ({place['rating_count']} reviews)")
            updated += 1
        else:
            logging.info(f"  {v['name']}: not found in Places")
            not_found += 1

    return {"places_updated": updated, "places_not_found": not_found}


# ---------------------------------------------------------------------------
# Phase 3: Classify date spots with Haiku
# ---------------------------------------------------------------------------

def _classify_date_spots_batch(names: list[str], anthropic_api_key: str) -> dict[str, str]:
    """Use Claude Haiku to classify venues as dinner date spots.

    Returns {name: 'yes' | 'no' | 'unknown'}.
    'no' = bar, brewery, fast food, counter service, brunch-only, etc.
    'yes' = sit-down dinner restaurant suitable for a date
    'unknown' = unclear from name alone
    """
    if not names:
        return {}

    client = anthropic.Anthropic(api_key=anthropic_api_key)
    result = {}
    chunks = [names[i:i + 60] for i in range(0, len(names), 60)]

    for chunk in chunks:
        numbered = "\n".join(f"{i+1}. {n}" for i, n in enumerate(chunk))
        prompt = (
            "I have a list of San Francisco restaurants on Resy. I want to filter to places suitable for a dinner date — "
            "sit-down restaurants with a proper menu, good ambiance, appropriate for a romantic evening.\n\n"
            "Classify each as:\n"
            "- 'yes': sit-down dinner restaurant, good for a date (e.g. Italian trattoria, sushi bar, tasting menu, gastropub with full menu)\n"
            "- 'no': primarily a bar, brewery, taproom, fast food, counter service, dessert shop, brunch spot, or not a dinner venue\n"
            "- 'unknown': can't tell from the name alone\n\n"
            "Respond ONLY with valid JSON: {\"Restaurant Name\": \"yes|no|unknown\", ...}\n\n"
            f"Venues:\n{numbered}"
        )
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            start, end = text.find("{"), text.rfind("}") + 1
            if start >= 0 and end > start:
                result.update(json.loads(text[start:end]))
        except Exception as e:
            logging.error(f"Haiku classification batch failed: {e} — marking as 'unknown'")
            result.update({n: "unknown" for n in chunk})

    return result


def sync_classify(config: dict, sheets_service) -> dict:
    """Batch-classify unclassified venues as dinner date spots using Haiku."""
    sheet_id = config["google"]["sheet_id"]
    anthropic_api_key = config.get("anthropic_api_key")
    if not anthropic_api_key:
        logging.warning("anthropic_api_key not set — skipping classification.")
        return {"classified": 0}

    venue_data = _load_venue_data(sheets_service, sheet_id)
    unclassified = [(rid, v) for rid, v in venue_data.items() if not v["date_spot"]]
    logging.info(f"sync_classify: {len(unclassified)} venues to classify")

    if not unclassified:
        return {"classified": 0}

    names = [v["name"] for _, v in unclassified]
    classifications = _classify_date_spots_batch(names, anthropic_api_key)

    updates = []
    for resy_id, v in unclassified:
        label = classifications.get(v["name"], "unknown")
        updates.append((v["row_index"], label))

    _update_date_spot_column(sheets_service, sheet_id, updates)
    logging.info(f"sync_classify: classified {len(updates)} venues")

    yes = sum(1 for _, l in updates if l == "yes")
    no = sum(1 for _, l in updates if l == "no")
    unknown = sum(1 for _, l in updates if l == "unknown")
    logging.info(f"  yes={yes}, no={no}, unknown={unknown}")
    return {"classified": len(updates), "yes": yes, "no": no, "unknown": unknown}


# ---------------------------------------------------------------------------
# Phase 4: Rebuild Restaurants tab
# ---------------------------------------------------------------------------

def rebuild(config: dict, sheets_service) -> dict:
    """Filter Venue Data → regenerate Restaurants tab.

    Includes venues where date_spot is 'yes' or 'unknown'.
    Excludes venues classified 'no' by Haiku (bars, breweries, etc.).
    Google Places rating filter is applied only when data is present.
    """
    sheet_id = config["google"]["sheet_id"]
    places_config = config.get("google_places", {})
    min_rating = places_config.get("min_rating", 4.3)
    min_reviews = places_config.get("min_rating_count", 40)

    venue_data = _load_venue_data(sheets_service, sheet_id)
    stale_cutoff = (datetime.date.today() - datetime.timedelta(days=60)).isoformat()
    logging.info(f"rebuild: {len(venue_data)} venues, excluding date_spot=no and unseen >60 days")

    passing_rows = []
    stale_count = 0
    for resy_id, v in venue_data.items():
        # Skip bars/breweries/non-date-spots
        if v["date_spot"] == "no":
            continue

        # Skip venues not seen in any Resy browse in the last 60 days
        if v["resy_date"] and v["resy_date"] < stale_cutoff:
            stale_count += 1
            continue

        # Apply rating filter only when Places data exists
        if v["g_rating"] and v["g_reviews"]:
            try:
                if float(v["g_rating"]) < min_rating or int(v["g_reviews"]) < min_reviews:
                    continue
            except ValueError:
                pass

        passing_rows.append([v["name"], resy_id, "Resy"])

    restaurants_tab = _get_restaurants_tab(config)
    sheets_service.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=f"{restaurants_tab}!A:C", body={}
    ).execute()

    if passing_rows:
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{restaurants_tab}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": passing_rows},
        ).execute()

    logging.info(f"rebuild: {len(passing_rows)} pass filter, {stale_count} stale (dropped from Resy)")
    return {"passing": len(passing_rows), "total": len(venue_data), "stale": stale_count}


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------

def run_pipeline(config: dict, gcal_service, sheets_service, drive_service=None, gmail_service=None) -> dict:
    counts = {}
    counts.update(sync_resy(config, sheets_service))
    counts.update(sync_classify(config, sheets_service))
    counts.update(rebuild(config, sheets_service))
    return counts


def _send_telegram_summary(config: dict, counts: dict):
    token = config.get("telegram", {}).get("bot_token")
    chat_id = config.get("telegram", {}).get("chat_id")
    if not token or not chat_id:
        return
    text = (
        f"🔍 *Pipeline Complete*\n\n"
        f"🏠 Resy: {counts.get('total_resy', 0)} total, {counts.get('new_resy', 0)} new\n"
        f"⭐ Places: {counts.get('places_updated', 0)} rated\n"
        f"🍽 Date spots: {counts.get('yes', 0)} yes / {counts.get('no', 0)} no / {counts.get('unknown', 0)} unknown\n"
        f"✅ In restaurant list: {counts.get('passing', 0)}/{counts.get('total', 0)}"
    )
    try:
        http_requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        logging.error(f"Telegram summary failed: {e}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    parser = argparse.ArgumentParser(description="Restaurant discovery pipeline")
    parser.add_argument("--sync-resy", action="store_true")
    parser.add_argument("--sync-places", action="store_true")
    parser.add_argument("--sync-classify", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    run_all = not any([args.sync_resy, args.sync_places, args.sync_classify, args.rebuild])

    with open(CONFIG_PATH) as f:
        config = json.load(f)

    gcal_service, sheets_service, gmail_service, drive_service = google_services.get_google_services()

    if args.sync_resy or run_all:
        logging.info("=== Phase 1: Sync Resy ===")
        logging.info(sync_resy(config, sheets_service))

    if args.sync_places:  # explicit flag only — skipped by default
        logging.info("=== Phase 2: Sync Places ===")
        logging.info(sync_places(config, sheets_service))

    if args.sync_classify or run_all:
        logging.info("=== Phase 3: Classify Date Spots ===")
        logging.info(sync_classify(config, sheets_service))

    if args.rebuild or run_all:
        logging.info("=== Phase 4: Rebuild ===")
        counts = rebuild(config, sheets_service)
        logging.info(counts)
        if run_all:
            _send_telegram_summary(config, counts)


if __name__ == "__main__":
    main()
