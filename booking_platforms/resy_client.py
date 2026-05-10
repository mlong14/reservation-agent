import requests
import json
import datetime
import os
import logging

# Base URL for Resy's API
RESY_API_URL = "https://api.resy.com"

def _resy_venue_search(api_key: str, query: str) -> str | None:
    """Raw Resy venue search — returns objectID of first hit or None."""
    headers = {"Authorization": f'ResyAPI api_key="{api_key}"'}
    payload = {
        "query": query,
        "types": ["venue"],
        "geo": {"latitude": 37.7749, "longitude": -122.4194, "radius": 32200},
    }
    try:
        resp = requests.post(f"{RESY_API_URL}/3/venuesearch/search", headers=headers, json=payload)
        resp.raise_for_status()
        hits = resp.json().get("search", {}).get("hits", [])
        return hits[0].get("objectID") if hits else None
    except requests.exceptions.RequestException as e:
        logging.error(f"Resy venue search error for '{query}': {e}")
        return None


def find_venue_id(resy_config: dict, restaurant_name: str):
    """Find Resy venue ID by name, with fallbacks for fuzzy name matching.

    Resy's search uses Algolia which can drop stop words like 'on', 'at', 'the'
    from multi-word queries. Tries progressively shorter name fragments.
    """
    api_key = resy_config["api_key"]

    # Try full name first
    result = _resy_venue_search(api_key, restaurant_name)
    if result:
        return result

    # Strip common location suffixes: "Kuma on Valencia" → "Kuma"
    for separator in (" on ", " at ", " - ", " & ", " and "):
        if separator.lower() in restaurant_name.lower():
            short = restaurant_name.split(separator)[0].strip()
            if short and short.lower() != restaurant_name.lower():
                result = _resy_venue_search(api_key, short)
                if result:
                    logging.info(f"  Found via shortened name '{short}' (from '{restaurant_name}')")
                    return result

    # Last resort: first two words
    words = restaurant_name.split()
    if len(words) > 2:
        short = " ".join(words[:2])
        result = _resy_venue_search(api_key, short)
        if result:
            logging.info(f"  Found via first-two-words '{short}' (from '{restaurant_name}')")
            return result

    return None

def browse_sf_venues(resy_config: dict) -> list[dict]:
    """Return all SF venues on Resy via paginated empty-query search.

    Uses the same payload as resy.com/cities/san-francisco-ca/search with
    availability=False to capture every venue, not just ones with open slots today.
    Each entry: {venue_id, name, url_slug}.
    """
    import time as _time
    api_key = resy_config["api_key"]
    headers = {"Authorization": f'ResyAPI api_key="{api_key}"'}
    seen_ids: set[str] = set()
    all_venues: list[dict] = []
    page = 1

    while True:
        payload = {
            "availability": False,
            "geo": {"latitude": 37.7577, "longitude": -122.4376, "radius": 32200},
            "page": page,
            "per_page": 100,
            "query": "",
            "types": ["venue"],
        }
        try:
            resp = requests.post(f"{RESY_API_URL}/3/venuesearch/search", headers=headers, json=payload)
            resp.raise_for_status()
            search = resp.json().get("search", {})
            hits = search.get("hits", [])
            nb_pages = search.get("nbPages", 1)
        except requests.exceptions.RequestException as e:
            logging.error(f"Resy browse error on page {page}: {e}")
            break

        for hit in hits:
            # Only SF proper (excludes Oakland, Berkeley, etc. within the radius)
            if hit.get("location", {}).get("code") != "sf":
                continue
            vid = hit.get("id", {}).get("resy")
            name = hit.get("name", "").strip()
            if not vid or not name:
                continue
            vid = str(vid)
            if vid not in seen_ids:
                seen_ids.add(vid)
                all_venues.append({"venue_id": vid, "name": name, "url_slug": hit.get("url_slug", "")})

        if page >= nb_pages:
            break
        page += 1
        _time.sleep(0.3)

    logging.info(f"Resy browse: {len(all_venues)} unique SF venues across {page} pages")
    return all_venues


def get_active_reservations(resy_config: dict):
    """
    Gets the user's upcoming reservations from Resy.

    Args:
        resy_config: A dictionary containing Resy API key and auth token.

    Returns:
        A list of active reservation objects.
    """
    headers = {
        "Authorization": f'ResyAPI api_key="{resy_config["api_key"]}"',
        "x-resy-auth-token": resy_config["auth_token"],
        "x-resy-universal-auth": resy_config["auth_token"], 
        "Content-Type": "application/json",
        "User-Agent": "Resy/3.12.0 (iPhone; iOS 13.3; Scale/2.00)",
        "X-Origin": "https://resy.com",
    }
    params = {
        "type": "upcoming"
    }
    try:
        response = requests.get(f"{RESY_API_URL}/3/user/reservations", headers=headers, params=params)
        response.raise_for_status()
        return response.json().get("reservations", [])
    except requests.exceptions.RequestException as e:
        logging.error(f"Error getting active reservations: {e}")
        if e.response:
            logging.error(f"Response Content: {e.response.text}")
        return [] # Return empty list on error to avoid breaking the agent

def find_slots(resy_config: dict, venue_id: int, party_size: int, date: str, preferred_times: dict, preferred_seating: list[str] = []):
    """
    Finds available reservation slots for a given restaurant on a specific date.

    Args:
        resy_config: A dictionary containing Resy API key and auth token.
        venue_id: The Resy venue ID of the restaurant.
        party_size: The number of people in the party.
        date: The date to search for reservations (YYYY-MM-DD).
        preferred_times: A dictionary with start_time and end_time.
        preferred_seating: A list of preferred seating types.

    Returns:
        A list of available slot configuration tokens.
    """
    headers = {
        "Authorization": f'ResyAPI api_key="{resy_config["api_key"]}"',
        "x-resy-auth-token": resy_config["auth_token"],
        "Content-Type": "application/json",
        "User-Agent": "Resy/3.12.0 (iPhone; iOS 13.3; Scale/2.00)"
    }
    params = {
        "venue_id": venue_id,
        "party_size": party_size,
        "day": date,
        "lat": 0,
        "long": 0
    }
    try:
        response = requests.get(f"{RESY_API_URL}/4/find", headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        
        venues = data.get("results", {}).get("venues")
        if not venues:
            logging.warning("No venue data found in the response.")
            return []

        slots = venues[0].get("slots", [])
        logging.info(f"Received {len(slots)} raw slot objects from API.")

        config_tokens = []
        start_time = datetime.datetime.strptime(preferred_times['start_time'], '%H:%M').time()
        end_time = datetime.datetime.strptime(preferred_times['end_time'], '%H:%M').time()

        logging.info("Available Times Found (after filtering):")
        for slot in slots:
            slot_time_str = slot.get('date', {}).get('start', ' ').split(' ')[1]
            slot_time = datetime.datetime.strptime(slot_time_str, '%H:%M:%S').time()
            seating_type = slot.get('config', {}).get('type')
            config_id = slot.get('config', {}).get('token')

            if config_id and (not preferred_seating or seating_type in preferred_seating) and start_time <= slot_time <= end_time:
                logging.info(f"  - {slot_time_str} ({seating_type})")
                config_tokens.append(config_id)

        if not config_tokens:
            logging.info("  - None matching your preferences.")

        return config_tokens

    except requests.exceptions.RequestException as e:
        logging.error(f"Error finding slots: {e}")
        if e.response:
            logging.error(f"Response Content: {e.response.text}")
        return []
    except (IndexError, KeyError) as e:
        logging.error(f"Could not parse response from Resy, structure may have changed: {e}")
        return []

def find_available_times(resy_config: dict, venue_id: int, party_size: int, date: str, preferred_times: dict, preferred_seating: list[str] = []) -> list[dict]:
    """Like find_slots but returns [{time, type}] dicts for display in proposals."""
    headers = {
        "Authorization": f'ResyAPI api_key="{resy_config["api_key"]}"',
        "x-resy-auth-token": resy_config["auth_token"],
        "Content-Type": "application/json",
        "User-Agent": "Resy/3.12.0 (iPhone; iOS 13.3; Scale/2.00)"
    }
    params = {"venue_id": venue_id, "party_size": party_size, "day": date, "lat": 0, "long": 0}
    try:
        response = requests.get(f"{RESY_API_URL}/4/find", headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        venues = data.get("results", {}).get("venues")
        if not venues:
            return []
        slots = venues[0].get("slots", [])
        start_t = datetime.datetime.strptime(preferred_times["start_time"], "%H:%M").time()
        end_t = datetime.datetime.strptime(preferred_times["end_time"], "%H:%M").time()
        result = []
        for slot in slots:
            slot_time_str = slot.get("date", {}).get("start", " ").split(" ")[1]
            slot_time = datetime.datetime.strptime(slot_time_str, "%H:%M:%S").time()
            seating_type = slot.get("config", {}).get("type", "")
            token = slot.get("config", {}).get("token")
            if token and (not preferred_seating or seating_type in preferred_seating) and start_t <= slot_time <= end_t:
                result.append({"time": slot_time_str[:5], "type": seating_type})
        return result
    except Exception as e:
        logging.error(f"Error finding available times for venue {venue_id}: {e}")
        return []


def book_slot(resy_config: dict, venue_id: int, party_size: int, date: str, preferred_times: dict, preferred_seating: list[str]):
    """
    Books a reservation slot.

    Args:
        resy_config: A dictionary containing Resy API key, auth token and payment method ID.
        venue_id: The Resy venue ID of the restaurant.
        party_size: The number of people in the party.
        date: The date of the reservation (YYYY-MM-DD).
        preferred_times: A dictionary with start_time and end_time.
        preferred_seating: A list of preferred seating types.

    Returns:
        A tuple containing the booking confirmation ID and the slot details if successful, otherwise None.
    """
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
        "Authorization": f'ResyAPI api_key="{resy_config["api_key"]}"',
        "Cache-Control": "no-cache",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://widgets.resy.com",
        "Priority": "u=1, i",
        "Referer": "https://widgets.resy.com/",
        "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="138", "Google Chrome";v="138"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        "X-Origin": "https://widgets.resy.com",
        "X-Resy-Auth-Token": resy_config["auth_token"],
        "x-resy-universal-auth": resy_config["auth_token"],    
    }

    with requests.Session() as session:
        session.headers.update(headers)

        find_params = {
            "venue_id": venue_id,
            "party_size": party_size,
            "day": date,
            "lat": 0,
            "long": 0
        }
        try:
            find_response = session.get(f"{RESY_API_URL}/4/find", params=find_params)
            find_response.raise_for_status()
            find_data = find_response.json()
            
            find_venues = find_data.get("results", {}).get("venues")
            if not find_venues:
                logging.warning("No venue data found when re-finding slot.")
                return None, None

            found_slots = find_venues[0].get("slots", [])
            slot_to_book = None
            start_time = datetime.datetime.strptime(preferred_times['start_time'], '%H:%M').time()
            end_time = datetime.datetime.strptime(preferred_times['end_time'], '%H:%M').time()

            for slot in found_slots:
                slot_time_str = slot.get('date', {}).get('start', ' ').split(' ')[1]
                slot_time = datetime.datetime.strptime(slot_time_str, '%H:%M:%S').time()
                seating_type = slot.get('config', {}).get('type')
                token = slot.get('config', {}).get('token')
                if token and (not preferred_seating or seating_type in preferred_seating) and start_time <= slot_time <= end_time:
                    slot_to_book = slot
                    break
            
            if not slot_to_book:
                logging.warning("Could not re-find a matching config_token for booking.")
                return None, None

            fresh_config_token = slot_to_book.get('config', {}).get('token')
            logging.info(f"Re-found fresh config_token: {fresh_config_token}")

            details_params = {"config_id": fresh_config_token, "day": date, "party_size": party_size}
            details_response = session.get(f"{RESY_API_URL}/3/details", params=details_params)
            details_response.raise_for_status()
            booking_token = details_response.json()['book_token']['value']
            logging.info(f"Received booking_token: {booking_token}")

            book_payload = {
                "book_token": booking_token,
                "struct_payment_method": json.dumps({'id': resy_config["resy_payment_method_id"]}),
                "venue_marketing_opt_in": "0"
            }
            book_response = session.post(f"{RESY_API_URL}/3/book", data=book_payload)
            book_response.raise_for_status()
            
            reservation_id = book_response.json().get("resy_token")
            logging.info(f"Successfully booked reservation! Confirmation ID: {reservation_id}")
            return reservation_id, slot_to_book
        except requests.exceptions.RequestException as e:
            logging.error(f"Error booking slot: {e}")
            if e.response is not None:
                logging.error(f"Response Status Code: {e.response.status_code}")
                logging.error(f"Response Content: {e.response.text}")
            return None, None


# --- Temporary Test Block ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    try:
        script_dir = os.path.dirname(__file__)
        config_path = os.path.abspath(os.path.join(script_dir, '..', 'config.json'))
        
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        resy_config = config['resy']
        user_config = config['user']
        
    except (FileNotFoundError, KeyError) as e:
        logging.error(f"Error loading credentials from config.json: {e}")
        exit()

    logging.info("--- Starting Resy Client Test ---")

    # Test find_venue_id
    logging.info("\n--- Testing find_venue_id ---")
    venue_id = find_venue_id(resy_config, "Izakaya Rintaro")
    if venue_id:
        logging.info(f"Found venue ID for 'Izakaya Rintaro': {venue_id}")
    else:
        logging.error("Could not find venue ID for 'Izakaya Rintaro'.")
    
    # Test get_active_reservations
    logging.info("\n--- Testing get_active_reservations ---")
    reservations = get_active_reservations(resy_config)
    if reservations:
        logging.info(f"Found {len(reservations)} active reservations:")
        for res in reservations:
            logging.info(json.dumps(res, indent=2))
    else:
        logging.info("No active reservations found.")

    # Test find_slots
    TEST_VENUE_ID = 339
    TEST_DATE = (datetime.date.today() + datetime.timedelta(days=20)).strftime("%Y-%m-%d")
    logging.info(f"\n--- Testing find_slots for venue {TEST_VENUE_ID} on {TEST_DATE} ---")
    available_slot_tokens = find_slots(
        resy_config,
        venue_id=TEST_VENUE_ID,
        party_size=user_config['party_size'],
        date=TEST_DATE,
        preferred_times=user_config['preferred_times'],
        preferred_seating=user_config.get('preferred_seating', [])
    )
    logging.info(f"Found {len(available_slot_tokens)} available slots.")

    # Test book_slot (will not actually book, just test the flow)
    if available_slot_tokens:
        logging.info("\n--- Testing book_slot (simulation) ---")
        # booking_id, slot_details = book_slot(resy_config, TEST_VENUE_ID, user_config['party_size'], TEST_DATE, user_config.get('preferred_seating', []))
        # if booking_id:
        #     logging.info(f"Simulated booking successful! ID: {booking_id}")
        #     logging.info(f"Slot details: {slot_details}")
        # else:
        #     logging.error("Simulated booking failed.")

    logging.info("--- Resy Client Test Finished ---")
