import json
import os
import uuid
import datetime
import logging

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
STATE_PATH = os.path.join(SCRIPT_DIR, "state.json")


class AgentState:
    def __init__(self):
        self._data = self._load()

    def _load(self):
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                logging.warning("Could not load state.json, starting fresh.")
        return {"pending_proposals": [], "booked_history": []}

    def _save(self):
        with open(STATE_PATH, "w") as f:
            json.dump(self._data, f, indent=2)

    # --- Proposals ---

    def add_proposal(self, restaurant: dict, date: str, time_str: str, party_size: int, ttl_hours: int = 12, note: str = "", llm_screened: bool = False) -> dict:
        proposal = {
            "id": str(uuid.uuid4()),
            "restaurant_name": restaurant["name"],
            "venue_id": restaurant.get("venue_id"),
            "platform": restaurant.get("platform", "").lower(),
            "date": date,
            "time": time_str,
            "party_size": party_size,
            "note": note,
            "llm_screened": llm_screened,
            "telegram_message_id": None,
            "proposed_at": datetime.datetime.now().isoformat(),
            "expires_at": (datetime.datetime.now() + datetime.timedelta(hours=ttl_hours)).isoformat(),
        }
        self._data["pending_proposals"].append(proposal)
        self._save()
        return proposal

    def set_proposal_message_id(self, proposal_id: str, message_id: int):
        for p in self._data["pending_proposals"]:
            if p["id"] == proposal_id:
                p["telegram_message_id"] = message_id
                self._save()
                return

    def remove_proposal(self, proposal_id: str):
        self._data["pending_proposals"] = [
            p for p in self._data["pending_proposals"] if p["id"] != proposal_id
        ]
        self._save()

    def get_proposal(self, proposal_id: str) -> dict | None:
        for p in self._data["pending_proposals"]:
            if p["id"] == proposal_id:
                return p
        return None

    def get_active_proposals(self) -> list[dict]:
        now = datetime.datetime.now().isoformat()
        active = [p for p in self._data["pending_proposals"] if p.get("expires_at", now) >= now]
        if len(active) != len(self._data["pending_proposals"]):
            self._data["pending_proposals"] = active
            self._save()
        return active

    # --- Bookings ---

    def add_booking(self, restaurant_name: str, date: str, confirmation_id: str, reservation_datetime_iso: str = None) -> dict:
        booking = {
            "id": str(uuid.uuid4()),
            "restaurant_name": restaurant_name,
            "date": date,
            "reservation_datetime": reservation_datetime_iso,
            "confirmation_id": confirmation_id,
            "booked_at": datetime.datetime.now().isoformat(),
            "feedback": None,
            "feedback_message_id": None,
            "feedback_requested": False,
            "reminder_sent": False,
        }
        self._data["booked_history"].append(booking)
        self._save()
        return booking

    def get_recent_restaurant_names(self, days: int = 60) -> set:
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
        return {b["restaurant_name"] for b in self._data["booked_history"] if b.get("booked_at", "") >= cutoff}

    def record_proposal_history(self, restaurant_name: str):
        history = self._data.setdefault("proposal_history", [])
        history.append({"restaurant_name": restaurant_name, "proposed_at": datetime.datetime.now().isoformat()})
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=60)).isoformat()
        self._data["proposal_history"] = [h for h in history if h.get("proposed_at", "") >= cutoff]
        self._save()

    def get_recently_proposed_restaurants(self, days: int = 14) -> set:
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
        return {h["restaurant_name"] for h in self._data.get("proposal_history", [])
                if h.get("proposed_at", "") >= cutoff}

    def get_bookings_needing_feedback(self) -> list[dict]:
        now = datetime.datetime.now().isoformat()
        return [
            b for b in self._data["booked_history"]
            if not b.get("feedback")
            and not b.get("feedback_requested")
            and b.get("reservation_datetime", "")
            and b["reservation_datetime"] < now
        ]

    def mark_feedback_requested(self, booking_id: str, message_id: int):
        for b in self._data["booked_history"]:
            if b["id"] == booking_id:
                b["feedback_requested"] = True
                b["feedback_message_id"] = message_id
                self._save()
                return

    def save_feedback(self, booking_id: str, sentiment: str):
        for b in self._data["booked_history"]:
            if b["id"] == booking_id:
                b["feedback"] = sentiment
                self._save()
                return

    def mark_reminder_sent(self, booking_id: str):
        for b in self._data["booked_history"]:
            if b["id"] == booking_id:
                b["reminder_sent"] = True
                self._save()
                return

    def get_bookings_needing_reminder(self, days_ahead: int = 3) -> list[dict]:
        target = (datetime.date.today() + datetime.timedelta(days=days_ahead)).isoformat()
        return [
            b for b in self._data["booked_history"]
            if b.get("date") == target and not b.get("reminder_sent")
        ]

    def get_booking(self, booking_id: str) -> dict | None:
        for b in self._data["booked_history"]:
            if b["id"] == booking_id:
                return b
        return None

    def get_all_bookings(self) -> list[dict]:
        return list(self._data["booked_history"])

    # --- Pipeline state ---

    def get_last_pipeline_zip(self) -> str | None:
        return self._data.get("last_pipeline_zip")

    def set_last_pipeline_zip(self, zip_name: str):
        self._data["last_pipeline_zip"] = zip_name
        self._save()

    def get_processed_resy_venue_ids(self) -> set:
        return set(str(v) for v in self._data.get("processed_resy_venue_ids", []))

    def add_processed_resy_venue_ids(self, venue_ids: list):
        existing = self._data.setdefault("processed_resy_venue_ids", [])
        str_existing = {str(v) for v in existing}
        added = [v for v in venue_ids if str(v) not in str_existing]
        existing.extend(added)
        if added:
            self._save()

    def get_processed_place_ids(self) -> set:
        return set(self._data.get("processed_place_ids", []))

    def add_processed_place_ids(self, place_ids: list[str]):
        existing = self._data.setdefault("processed_place_ids", [])
        added = [pid for pid in place_ids if pid not in existing]
        existing.extend(added)
        if added:
            self._save()

    def get_monthly_places_api_calls(self) -> int:
        key = datetime.date.today().strftime("%Y-%m")
        return self._data.get("places_api_calls", {}).get(key, 0)

    def record_places_api_calls(self, count: int):
        key = datetime.date.today().strftime("%Y-%m")
        calls = self._data.setdefault("places_api_calls", {})
        calls[key] = calls.get(key, 0) + count
        self._save()
