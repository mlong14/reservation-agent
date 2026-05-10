"""Quick test: check auth token and find slots for a single restaurant."""
import json
import datetime
import logging
import sys
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
with open(os.path.join(SCRIPT_DIR, "config.json")) as f:
    config = json.load(f)

from booking_platforms import resy_client

resy_config = config["resy"]
user_config = config["user"]

# --- Config ---
VENUE_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 339  # pass venue ID as arg
DAYS_AHEAD = 14

print(f"\nTesting venue_id={VENUE_ID} for the next {DAYS_AHEAD} days...\n")

found_any = False
for i in range(1, DAYS_AHEAD + 1):
    date = (datetime.date.today() + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
    times = resy_client.find_available_times(
        resy_config,
        venue_id=VENUE_ID,
        party_size=user_config["party_size"],
        date=date,
        preferred_times=user_config["preferred_times"],
        preferred_seating=user_config.get("preferred_seating", []),
    )
    if times:
        found_any = True
        print(f"  {date}: {[t['time'] + ' (' + t['type'] + ')' for t in times]}")

if not found_any:
    print("  No availability found in that window (or auth error above).")
