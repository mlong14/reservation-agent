# Personal Reservation Agent

A Telegram-based assistant that automatically finds and books restaurant reservations on Resy, checking your Google Calendar for conflicts and keeping a curated restaurant list in a Google Sheet.

## How it works

1. **Discovery pipeline** — browses all SF restaurants on Resy, classifies them with Claude Haiku (date-spot vs bar/brewery/counter service), and stores results in a Google Sheet
2. **Proposal agent** — runs Mon/Wed/Fri, checks your calendar for free evenings, scores them with Claude (avoiding ±2 days of existing reservations), and sends 3 proposals to Telegram
3. **Booking** — tap "Book it" in Telegram, the agent books on Resy and adds the event to Google Calendar
4. **Follow-up** — 3-day reminders before reservations, feedback requests after

## Architecture

```
telegram_bot.py          — Telegram bot (main interface, scheduled jobs)
agent.py                 — Proposal logic, calendar scoring, booking execution
restaurant_pipeline.py   — Restaurant discovery & classification pipeline
booking_platforms/
  resy_client.py         — Resy API (browse, search, book)
google_services.py       — Google Calendar, Sheets, Gmail, Drive
discovery.py             — Google Places API helpers
state.py                 — Local JSON state (proposals, bookings, history)
```

## Google Sheet structure

Two tabs:
- **Venue Data** — raw store: every SF Resy venue with rating, classification, last-seen date
- **Restaurants** — filtered output: venues passing all filters (date-spot, not stale, optional rating threshold)

## Setup

### 1. Clone and install

```bash
git clone <repository-url>
cd reservation-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Google API credentials

1. Create a project in [Google Cloud Console](https://console.cloud.google.com)
2. Enable: Google Calendar API, Google Sheets API, Gmail API, Google Drive API, Google Places API (New)
3. Create OAuth 2.0 credentials → download as `credentials.json`
4. Run `python google_services.py` once to complete OAuth flow (creates `token.json`)

### 3. Resy credentials

Capture your Resy API key and auth token from browser DevTools (Network tab on resy.com).

### 4. config.json

Copy and fill in `config.json` (never commit this file):

```json
{
  "resy": {
    "api_key": "...",
    "auth_token": "...",
    "resy_payment_method_id": 0
  },
  "user": {
    "party_size": 2,
    "preferred_days": ["Friday", "Thursday"],
    "preferred_times": { "start_time": "18:30", "end_time": "20:00" },
    "day_time_overrides": {
      "Thursday": { "start_time": "19:00", "end_time": "20:00" }
    },
    "preferred_seating": ["Indoor", "Bar"],
    "timezone": "America/Los_Angeles",
    "reservation_target": 2,
    "proposal_count": 3
  },
  "google": {
    "sheet_id": "...",
    "restaurants_tab": "Restaurants",
    "calendar_ids": ["your@gmail.com"],
    "event_calendar_id": "your@gmail.com"
  },
  "email": { "recipient": "your@gmail.com" },
  "google_places": {
    "api_key": "...",
    "min_rating": 4.3,
    "min_rating_count": 40,
    "monthly_call_limit": 900
  },
  "anthropic_api_key": "...",
  "telegram": {
    "bot_token": "...",
    "chat_id": "..."
  }
}
```

### 5. Telegram bot

1. Create a bot via [@BotFather](https://t.me/BotFather), copy the token into `config.json`
2. Start the bot, send it any message, then get your chat ID via the Telegram API

### 6. Run as a background service (macOS)

Edit `com.mwlong.reservation-agent.plist` to replace the username and paths for your system, then:

```bash
sudo cp com.mwlong.reservation-agent.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/com.mwlong.reservation-agent.plist
sudo launchctl load /Library/LaunchDaemons/com.mwlong.reservation-agent.plist
```

To restart after code changes:
```bash
sudo launchctl unload /Library/LaunchDaemons/com.mwlong.reservation-agent.plist
sudo launchctl load /Library/LaunchDaemons/com.mwlong.reservation-agent.plist
```

## Telegram commands

| Command | Description |
|---|---|
| `/find` | Search for available slots and send proposals now |
| `/status` | Show pending proposals and upcoming reservations |
| `/history` | Past bookings with feedback |
| `/pipeline` | Run restaurant discovery pipeline manually |

## Pipeline CLI

```bash
# Full run (Resy sync + classify + rebuild) — default
.venv/bin/python restaurant_pipeline.py

# Individual phases
.venv/bin/python restaurant_pipeline.py --sync-resy
.venv/bin/python restaurant_pipeline.py --sync-classify
.venv/bin/python restaurant_pipeline.py --rebuild

# Google Places rating sync (rate-limited, explicit flag only)
.venv/bin/python restaurant_pipeline.py --sync-places
```

## Scheduled jobs

| Schedule | Job |
|---|---|
| Mon/Wed/Fri 9:10 AM | Auto-propose if below `reservation_target` |
| 1st of month 2:00 AM | Pipeline (Resy sync + classify + rebuild) |
| Daily 9:00 AM | 3-day reservation reminders |
| Daily 9:05 AM | Post-dinner feedback requests |
