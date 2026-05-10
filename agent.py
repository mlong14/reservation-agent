import json
import random
import os
import logging
import argparse
import time
import datetime
import requests as http_requests

import anthropic
import pytz
from booking_platforms import resy_client
import google_services
from state import AgentState

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


def fmt_time(time_str: str) -> str:
    """Convert HH:MM to 12-hour format, e.g. '18:30' → '6:30 PM'."""
    t = datetime.datetime.strptime(time_str, "%H:%M")
    return t.strftime("%-I:%M %p")


def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


# --- Telegram helpers (used by cron/agent runs; bot uses its own async API) ---

def _telegram_post(config, endpoint, payload):
    token = config.get("telegram", {}).get("bot_token")
    if not token:
        return None
    try:
        r = http_requests.post(f"https://api.telegram.org/bot{token}/{endpoint}", json=payload, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.error(f"Telegram API error ({endpoint}): {e}")
        return None


def _send_telegram_text(config, text):
    chat_id = config.get("telegram", {}).get("chat_id")
    if not chat_id:
        logging.warning("Telegram chat_id not configured.")
        return
    _telegram_post(config, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})


def _send_telegram_proposal(config, proposal):
    chat_id = config.get("telegram", {}).get("chat_id")
    if not chat_id:
        logging.warning("Telegram chat_id not configured — cannot send proposal.")
        return

    date_display = datetime.datetime.strptime(proposal["date"], "%Y-%m-%d").strftime("%A, %B %-d")
    note = proposal.get("note", "")
    note_line = f"\n\n⚠️ _{note}_" if note else ""
    screen_line = "\n🤖 _Calendar screened_" if proposal.get("llm_screened") else "\n📋 _Basic calendar check only_"
    text = (
        f"🍽 *Reservation Proposal*\n\n"
        f"📍 *{proposal['restaurant_name']}*\n"
        f"📅 {date_display} at {fmt_time(proposal['time'])}\n"
        f"👥 {proposal['party_size']} people\n"
        f"🎫 {proposal['platform'].title()}"
        f"{note_line}"
        f"{screen_line}"
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Book it!", "callback_data": f"book_{proposal['id']}"},
            {"text": "⏭ Skip", "callback_data": f"skip_{proposal['id']}"},
        ]]
    }
    result = _telegram_post(config, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": keyboard,
    })
    if result and result.get("ok"):
        msg_id = result["result"]["message_id"]
        AgentState().set_proposal_message_id(proposal["id"], msg_id)


def _send_reminder(config, booking):
    chat_id = config.get("telegram", {}).get("chat_id")
    if not chat_id:
        return
    res_dt = booking.get("reservation_datetime")
    time_str = datetime.datetime.fromisoformat(res_dt).strftime("%-I:%M %p") if res_dt else ""
    date_str = datetime.datetime.strptime(booking["date"], "%Y-%m-%d").strftime("%A, %B %-d")
    result = _telegram_post(config, "sendMessage", {
        "chat_id": chat_id,
        "text": (
            f"⏰ *3-day reminder:* Dinner at *{booking['restaurant_name']}*\n"
            f"📅 {date_str} at {time_str}\n\n"
            f"Anything to change?"
        ),
        "parse_mode": "Markdown",
    })
    if result and result.get("ok"):
        AgentState().mark_reminder_sent(booking["id"])


def _send_feedback_request(config, booking):
    chat_id = config.get("telegram", {}).get("chat_id")
    if not chat_id:
        return
    keyboard = {
        "inline_keyboard": [[
            {"text": "👍 Great!", "callback_data": f"feedback_{booking['id']}_good"},
            {"text": "👎 Not great", "callback_data": f"feedback_{booking['id']}_bad"},
        ]]
    }
    result = _telegram_post(config, "sendMessage", {
        "chat_id": chat_id,
        "text": f"🍽 How was dinner at *{booking['restaurant_name']}*?",
        "parse_mode": "Markdown",
        "reply_markup": keyboard,
    })
    if result and result.get("ok"):
        AgentState().mark_feedback_requested(booking["id"], result["result"]["message_id"])


# --- Core logic ---

def _score_candidate_dates(candidate_evenings: list, gcal_service, google_config: dict, user_config: dict, anthropic_api_key: str = None) -> dict:
    """Use Claude to rate each candidate evening as good / uncertain / skip.

    Fetches ±2 days of calendar events for context so Claude can spot travel,
    adjacent big dinners, etc. Returns {date_str: {score, note}}.
    """
    client = anthropic.Anthropic(api_key=anthropic_api_key) if anthropic_api_key else anthropic.Anthropic()
    calendar_ids = google_config["calendar_ids"]
    timezone = user_config["timezone"]

    # Build per-date event context
    date_contexts = {}
    for evening in candidate_evenings:
        target_date = evening.date()
        context_start = target_date - datetime.timedelta(days=2)
        context_end = target_date + datetime.timedelta(days=2)
        events = google_services.get_events_for_date_range(
            gcal_service, calendar_ids, context_start, context_end, timezone
        )
        date_contexts[target_date.isoformat()] = events

    # Build prompt
    blocks = []
    for date_str, events in date_contexts.items():
        day_label = datetime.date.fromisoformat(date_str).strftime("%A, %B %-d")
        blocks.append(f"Candidate: {day_label} ({date_str})")
        if events:
            for e in events:
                day = e["start"][:10]
                blocks.append(f"  [{day}] {e['summary']}")
        else:
            blocks.append("  (no events in ±2 day window)")

    prompt = (
        "You are helping Matt and Cindy (a couple in San Francisco) choose good evenings "
        "for a restaurant reservation. Review each candidate evening and rate it.\n\n"
        "Rating rules:\n"
        "- 'skip': clear dealbreaker — travel/overnight stay, flight that day, multi-day trip, "
        "OR a restaurant/dinner reservation on the candidate date itself or the day before/after.\n"
        "- 'uncertain': worth flagging — wedding/event that might run late, back-to-back social "
        "events, anything that warrants a human judgment call but isn't a clear skip.\n"
        "- 'good': nothing notable, looks like a fine evening for dinner.\n\n"
        "Important: treat any existing restaurant reservation (e.g. 'Dinner at X', 'X @7PM') "
        "within ±2 days of the candidate as a hard 'skip' — they should not have dinner out "
        "within 2 days of another reservation.\n\n"
        "For 'skip' and 'uncertain', write a concise one-line note explaining why. "
        "For 'good', note should be an empty string.\n\n"
        "Respond ONLY with valid JSON, no other text:\n"
        '{"YYYY-MM-DD": {"score": "good|uncertain|skip", "note": "..."}, ...}\n\n'
        "Calendar context (events shown are within ±2 days of each candidate):\n\n"
        + "\n".join(blocks)
    )

    fallback = ({d: {"score": "good", "note": ""} for d in date_contexts}, False)
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end]), True
    except Exception as e:
        logging.error(f"Claude date scoring failed, treating all dates as good: {e}")

    return fallback


def find_and_propose(config, gcal_service, gsheets_service, max_proposals=2) -> list[dict]:
    """Find available reservation slots and save them as proposals. Returns the proposal list."""
    state = AgentState()
    resy_config = config["resy"]
    user_config = config["user"]
    google_config = config["google"]

    # Fetch active Resy reservations for date/restaurant exclusion
    active_resy = []
    try:
        active_resy = resy_client.get_active_reservations(resy_config)
    except Exception as e:
        logging.warning(f"Could not fetch active Resy reservations: {e}")

    reserved_dates = set()
    actively_reserved_restaurants = set()
    for r in active_resy:
        if r.get("day"):
            reserved_dates.add(datetime.date.fromisoformat(r["day"]))
        share_msg = r.get("share", {}).get("generic_message", "")
        if "Please RSVP for " in share_msg and " on " in share_msg:
            actively_reserved_restaurants.add(share_msg.replace("Please RSVP for ", "").split(" on ")[0])

    free_evenings = google_services.find_free_evenings(gcal_service, user_config, google_config, days_to_check=45)
    if not free_evenings:
        logging.info("No free evenings found.")
        return []

    # Pre-filter dates within ±2 days of an existing Resy reservation
    def too_close_to_reservation(d: datetime.date) -> bool:
        return any(abs((d - rd).days) <= 2 for rd in reserved_dates)

    free_evenings = [e for e in free_evenings if not too_close_to_reservation(e.date())]
    if not free_evenings:
        logging.info("All candidate dates are within ±2 days of an existing reservation.")
        return []

    # Score remaining candidate dates with Claude (±2 day calendar context)
    logging.info(f"Scoring {len(free_evenings)} candidate evenings with Claude...")
    date_scores, llm_screened = _score_candidate_dates(
        free_evenings, gcal_service, google_config, user_config,
        anthropic_api_key=config.get("anthropic_api_key"),
    )

    # Filter out skips; sort good before uncertain
    scored = []
    for evening in free_evenings:
        score_info = date_scores.get(evening.strftime("%Y-%m-%d"), {"score": "good", "note": ""})
        if score_info["score"] != "skip":
            scored.append((evening, score_info))
    scored.sort(key=lambda x: 0 if x[1]["score"] == "good" else 1)

    if not scored:
        logging.info("All candidate dates scored as skip.")
        return []

    restaurants = google_services.get_restaurants_from_sheet(gsheets_service, google_config)
    if not restaurants:
        logging.info("No restaurants in list.")
        return []

    recently_booked = state.get_recent_restaurant_names(days=60)
    recently_proposed = state.get_recently_proposed_restaurants(days=14)
    excluded = recently_booked | recently_proposed | actively_reserved_restaurants
    bookable = [r for r in restaurants if r.get("platform", "").lower() not in ("", "unknown", None)]
    fresh = [r for r in bookable if r["name"] not in excluded]
    candidates = fresh if fresh else bookable

    # Shuffle restaurants but keep date order (good dates first)
    random.shuffle(candidates)
    combos = [(evening, score_info, restaurant) for evening, score_info in scored for restaurant in candidates]

    proposals = []
    proposed_restaurants = set()

    for evening, score_info, restaurant in combos:
        if len(proposals) >= max_proposals:
            break
        if restaurant["name"] in proposed_restaurants:
            continue

        date_str = evening.strftime("%Y-%m-%d")
        platform = restaurant.get("platform", "").lower()
        day_name = evening.strftime("%A")
        # Use per-day time override for the actual preferred_times passed to Resy
        day_times = user_config.get("day_time_overrides", {}).get(day_name, user_config["preferred_times"])

        if platform == "resy":
            try:
                times = resy_client.find_available_times(
                    resy_config,
                    venue_id=int(restaurant["venue_id"]),
                    party_size=user_config["party_size"],
                    date=date_str,
                    preferred_times=day_times,
                    preferred_seating=user_config.get("preferred_seating", []),
                )
                if times:
                    best_time = times[0]["time"]
                    proposal = state.add_proposal(
                        restaurant, date_str, best_time, user_config["party_size"],
                        note=score_info.get("note", ""),
                        llm_screened=llm_screened,
                    )
                    proposals.append(proposal)
                    proposed_restaurants.add(restaurant["name"])
                    state.record_proposal_history(restaurant["name"])
                    logging.info(f"Proposed: {restaurant['name']} on {date_str} at {best_time} [{score_info['score']}]")
            except Exception as e:
                logging.error(f"Error checking {restaurant['name']}: {e}", exc_info=True)


    return proposals


def do_booking(proposal: dict, config: dict, gcal_service) -> dict:
    """Execute a confirmed booking. Called by the Telegram bot when user taps Book it."""
    user_config = config["user"]
    google_config = config["google"]
    platform = proposal["platform"]

    booking_id = None
    slot_details = None

    if platform == "resy":
        booking_id, slot_details = resy_client.book_slot(
            config["resy"],
            venue_id=int(proposal["venue_id"]),
            party_size=proposal["party_size"],
            date=proposal["date"],
            preferred_times=user_config["preferred_times"],
            preferred_seating=user_config.get("preferred_seating", []),
        )

    if not booking_id or not slot_details:
        return {"success": False, "error": "Slot no longer available."}

    # Parse actual reservation time from slot details
    local_tz = pytz.timezone(user_config["timezone"])
    res_date = datetime.datetime.strptime(proposal["date"], "%Y-%m-%d")

    if platform == "resy":
        time_str = slot_details.get("date", {}).get("start", " ").split(" ")[1]
        res_time = datetime.datetime.strptime(time_str, "%H:%M:%S").time()
    else:
        h, m = map(int, proposal["time"].split(":"))
        res_time = datetime.time(h, m)

    reservation_dt = local_tz.localize(res_date.replace(hour=res_time.hour, minute=res_time.minute))

    created_event = google_services.create_calendar_event(
        gcal_service,
        start_time=reservation_dt,
        restaurant_name=proposal["restaurant_name"],
        party_size=proposal["party_size"],
        google_config=google_config,
    )

    state = AgentState()
    booking = state.add_booking(
        proposal["restaurant_name"],
        proposal["date"],
        booking_id,
        reservation_dt.isoformat(),
    )

    return {
        "success": True,
        "confirmation_id": booking_id,
        "reservation_datetime": reservation_dt,
        "calendar_link": created_event.get("htmlLink", "") if created_event else "",
        "booking": booking,
    }


# --- Agent run modes ---

def propose_agent(config, gcal_service, gsheets_service, gmail_service):
    """Default mode: find slots and send Telegram proposals for confirmation."""
    state = AgentState()
    target = config.get("user", {}).get("reservation_target", 2)
    proposal_count = config.get("user", {}).get("proposal_count", 3)

    if state.get_active_proposals():
        logging.info("Active proposals already pending. Skipping search.")
        return

    try:
        active = resy_client.get_active_reservations(config["resy"])
        if len(active) >= target:
            logging.info(f"Already have {len(active)} reservation(s) (target: {target}). Not proposing.")
            return
    except Exception as e:
        logging.error(f"Error checking existing reservations: {e}")

    proposals = find_and_propose(config, gcal_service, gsheets_service, max_proposals=proposal_count)

    if not proposals:
        logging.info("No available slots found matching preferences.")
        _send_telegram_text(config, "😕 No available reservation slots found this week.")
        return

    for proposal in proposals:
        _send_telegram_proposal(config, proposal)

    # Send pending feedback requests and 3-day reminders
    for booking in state.get_bookings_needing_feedback():
        _send_feedback_request(config, booking)
    for booking in state.get_bookings_needing_reminder():
        _send_reminder(config, booking)


def run_agent(config, gcal_service, gsheets_service, gmail_service):
    """Legacy auto-book mode (--auto-book flag). Books without confirmation."""
    logging.info("--- Running Reservation Agent (auto-book mode) ---")
    resy_config = config["resy"]
    user_config = config["user"]
    google_config = config["google"]
    email_config = config["email"]

    try:
        active_reservations = resy_client.get_active_reservations(resy_config)
        if active_reservations:
            logging.info(f"Found {len(active_reservations)} active reservation(s). Stopping.")
            return
    except Exception as e:
        logging.error(f"Error checking reservations: {e}")
        return

    free_evenings = google_services.find_free_evenings(gcal_service, user_config, google_config)
    if not free_evenings:
        logging.info("No free evenings found.")
        return

    restaurants = google_services.get_restaurants_from_sheet(gsheets_service, google_config)
    if not restaurants:
        logging.info("No restaurants in list.")
        return

    state = AgentState()
    recently_booked = state.get_recent_restaurant_names(days=60)
    bookable = [r for r in restaurants if r.get("platform", "").lower() not in ("", "unknown", None)]
    fresh = [r for r in bookable if r["name"] not in recently_booked]
    candidates = fresh if fresh else bookable

    combos = [(evening, restaurant) for evening in free_evenings for restaurant in candidates]
    random.shuffle(combos)

    for evening, restaurant in combos:
        date_str = evening.strftime("%Y-%m-%d")
        platform = restaurant.get("platform", "").lower()

        if platform == "resy":
            try:
                booking_id, slot_details = resy_client.book_slot(
                    resy_config,
                    venue_id=int(restaurant["venue_id"]),
                    party_size=user_config["party_size"],
                    date=date_str,
                    preferred_times=user_config["preferred_times"],
                    preferred_seating=user_config.get("preferred_seating", []),
                )
                if booking_id and slot_details:
                    time_str = slot_details.get("date", {}).get("start", " ").split(" ")[1]
                    res_time = datetime.datetime.strptime(time_str, "%H:%M:%S").time()
                    local_tz = pytz.timezone(user_config["timezone"])
                    reservation_dt = local_tz.localize(
                        evening.replace(hour=res_time.hour, minute=res_time.minute)
                    )
                    created_event = google_services.create_calendar_event(
                        gcal_service,
                        start_time=reservation_dt,
                        restaurant_name=restaurant["name"],
                        party_size=user_config["party_size"],
                        google_config=google_config,
                    )
                    state.add_booking(restaurant["name"], date_str, booking_id, reservation_dt.isoformat())
                    subject = f"Reservation Confirmed: {restaurant['name']} on {reservation_dt.strftime('%A, %B %d at %I:%M %p %Z')}"
                    body = (
                        f"Booked {user_config['party_size']} at {restaurant['name']} on "
                        f"{reservation_dt.strftime('%A, %B %d at %I:%M %p %Z')}.\n\n"
                        f"Confirmation: {booking_id}\n"
                        f"Calendar: {created_event.get('htmlLink') if created_event else 'N/A'}"
                    )
                    google_services.send_email(gmail_service, email_config["recipient"], subject, body)
                    logging.info("--- Booking complete ---")
                    return
            except Exception as e:
                logging.error(f"Error booking {restaurant['name']}: {e}", exc_info=True)

    logging.info("--- No reservations could be booked ---")


def update_restaurant_list(config, gsheets_service):
    """Find missing venue IDs and update the Google Sheet."""
    logging.info("--- Updating Restaurant List ---")
    restaurants = google_services.get_restaurants_from_sheet(gsheets_service, config["google"])
    for restaurant in restaurants:
        if not restaurant.get("venue_id") and not restaurant.get("platform"):
            logging.info(f"Searching for venue ID: {restaurant['name']}")
            venue_id = resy_client.find_venue_id(config["resy"], restaurant["name"])
            time.sleep(random.uniform(1, 3))
            if venue_id:
                google_services.update_restaurant_in_sheet(
                    gsheets_service, config["google"], restaurant["row_index"], venue_id, "Resy"
                )
            else:
                google_services.update_restaurant_in_sheet(
                    gsheets_service, config["google"], restaurant["row_index"], "", "Unknown"
                )


def interactive_mode(config, gcal_service, gsheets_service, gmail_service):
    while True:
        print("\n--- Menu ---")
        print("1. Find and propose reservations (Telegram)")
        print("2. Auto-book (no confirmation)")
        print("3. View upcoming Resy reservations")
        print("4. View free calendar slots")
        print("5. Update restaurant list")
        print("6. Exit")
        choice = input("Choice: ").strip()

        if choice == "1":
            propose_agent(config, gcal_service, gsheets_service, gmail_service)
        elif choice == "2":
            run_agent(config, gcal_service, gsheets_service, gmail_service)
        elif choice == "3":
            active = resy_client.get_active_reservations(config["resy"])
            print(json.dumps(active, indent=2) if active else "No upcoming reservations.")
        elif choice == "4":
            slots = google_services.find_free_evenings(gcal_service, config["user"], config["google"], days_to_check=60)
            for s in slots:
                print(s.strftime("%A, %B %d, %Y at %I:%M %p"))
        elif choice == "5":
            update_restaurant_list(config, gsheets_service)
        elif choice == "6":
            break


def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="Personal Reservation Agent")
    parser.add_argument("-i", "--interactive", action="store_true", help="Interactive mode")
    parser.add_argument("--auto-book", action="store_true", help="Auto-book without Telegram confirmation")
    parser.add_argument("--update-list", action="store_true", help="Update restaurant venue IDs in sheet")
    args = parser.parse_args()

    try:
        config = load_config()
    except (FileNotFoundError, KeyError) as e:
        logging.error(f"Config error: {e}")
        return

    logging.info("Connecting to Google Services...")
    gcal_service, gsheets_service, gmail_service, _ = google_services.get_google_services()
    if not gcal_service:
        logging.error("Failed to connect to Google Services.")
        return

    if args.interactive:
        interactive_mode(config, gcal_service, gsheets_service, gmail_service)
    elif args.auto_book:
        run_agent(config, gcal_service, gsheets_service, gmail_service)
    elif args.update_list:
        update_restaurant_list(config, gsheets_service)
    else:
        propose_agent(config, gcal_service, gsheets_service, gmail_service)


if __name__ == "__main__":
    main()
