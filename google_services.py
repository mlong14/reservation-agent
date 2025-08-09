import os
import json
import datetime
import base64
import logging
from email.mime.text import MIMEText
from dateutil import parser
import pytz
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- Constants ---
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "token.json")
CREDENTIALS_PATH = os.path.join(SCRIPT_DIR, "credentials.json")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/gmail.send"]

def get_google_services():
    """Authenticates with Google and returns Calendar, Sheets, and Gmail API services."""
    creds = None
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "r") as token_file:
            token_data = json.load(token_file)
        if set(token_data.get("scopes", [])) == set(SCOPES):
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        else:
            logging.warning("Token scopes have changed. Re-authenticating.")
            os.remove(TOKEN_PATH)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                logging.error(f"Error refreshing token: {e}")
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
                creds = flow.run_local_server(port=0)
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as token:
            token.write(creds.to_json())

    try:
        gsheets_service = build("sheets", "v4", credentials=creds)
        gcal_service = build("calendar", "v3", credentials=creds)
        gmail_service = build("gmail", "v1", credentials=creds)
        return gcal_service, gsheets_service, gmail_service
    except HttpError as err:
        logging.error(f"Error building Google services: {err}")
        return None, None, None

def get_restaurants_from_sheet(gsheets_service, google_config: dict):
    """Reads the restaurant list from the Google Sheet."""
    try:
        sheet = gsheets_service.spreadsheets()
        result = sheet.values().get(spreadsheetId=google_config['sheet_id'], range="A:C").execute()
        values = result.get("values", [])
        if not values:
            return []
        restaurants = []
        for i, row in enumerate(values[1:]):
            if len(row) >= 1:
                restaurants.append({
                    "name": row[0],
                    "venue_id": row[1] if len(row) > 1 else None,
                    "platform": row[2] if len(row) > 2 else None,
                    "row_index": i + 2 # 1-based index for sheets, plus 1 for header
                })
        return restaurants
    except HttpError as err:
        logging.error(f"An error occurred fetching from Google Sheets: {err}")
        return []

def update_restaurant_in_sheet(gsheets_service, google_config: dict, row_index: int, venue_id: str, platform: str):
    """Updates a restaurant's venue ID and platform in the Google Sheet."""
    try:
        body = {
            'values': [[venue_id, platform]]
        }
        gsheets_service.spreadsheets().values().update(
            spreadsheetId=google_config['sheet_id'],
            range=f"B{row_index}:C{row_index}",
            valueInputOption="USER_ENTERED",
            body=body
        ).execute()
        logging.info(f"Updated row {row_index} with venue_id={venue_id} and platform={platform}")
    except HttpError as err:
        logging.error(f"An error occurred updating the sheet: {err}")



def find_free_evenings(gcal_service, user_config: dict, google_config: dict, days_to_check=14, reservation_duration_hours=2):
    """Finds free time slots across all user calendars based on preferences."""
    user_timezone = user_config['timezone']
    preferred_days = user_config['preferred_days']
    start_time_str = user_config['preferred_times']['start_time']
    end_time_str = user_config['preferred_times']['end_time']
    calendar_ids = google_config['calendar_ids']

    local_tz = pytz.timezone(user_timezone)
    now_local = datetime.datetime.now(local_tz)
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    time_min = now_utc.isoformat()
    time_max = (now_utc + datetime.timedelta(days=days_to_check)).isoformat()

    try:
        freebusy_query = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": cal_id} for cal_id in calendar_ids]
        }
        freebusy_result = gcal_service.freebusy().query(body=freebusy_query).execute()
        
        all_busy_slots = []
        for cal_id in calendar_ids:
            all_busy_slots.extend(freebusy_result.get('calendars', {}).get(cal_id, {}).get('busy', []))

        free_slots = []
        start_hour, start_minute = map(int, start_time_str.split(':'))
        end_hour, end_minute = map(int, end_time_str.split(':'))

        for day_offset in range(days_to_check):
            current_day_local = now_local + datetime.timedelta(days=day_offset)
            day_name = current_day_local.strftime('%A')

            if day_name in preferred_days:
                potential_start_local = current_day_local.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
                end_of_window = current_day_local.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)

                while potential_start_local < end_of_window:
                    potential_end_local = potential_start_local + datetime.timedelta(hours=reservation_duration_hours)

                    if potential_start_local < now_local:
                        potential_start_local += datetime.timedelta(minutes=15)
                        continue

                    potential_start_utc = potential_start_local.astimezone(datetime.timezone.utc)
                    potential_end_utc = potential_end_local.astimezone(datetime.timezone.utc)

                    is_free = True
                    for busy in all_busy_slots:
                        busy_start_utc = parser.isoparse(busy['start'])
                        busy_end_utc = parser.isoparse(busy['end'])
                        if max(potential_start_utc, busy_start_utc) < min(potential_end_utc, busy_end_utc):
                            is_free = False
                            break
                    
                    if is_free:
                        free_slots.append(potential_start_local)
                    
                    potential_start_local += datetime.timedelta(minutes=15)
        
        return free_slots
        
    except HttpError as err:
        logging.error(f"An error occurred with Google Calendar API: {err}")
        return []

def create_calendar_event(gcal_service, start_time, restaurant_name, party_size, google_config: dict):
    """Creates a new event in the user's specified Google Calendar."""
    end_time = start_time + datetime.timedelta(hours=2)
    event = {
        'summary': f'Dinner at {restaurant_name}',
        'location': restaurant_name,
        'description': f'Reservation for {party_size}.',
        'start': {'dateTime': start_time.isoformat(), 'timeZone': str(start_time.tzinfo)},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': str(end_time.tzinfo)},
    }
    try:
        created_event = gcal_service.events().insert(calendarId=google_config.get('event_calendar_id', 'primary'), body=event).execute()
        logging.info(f"Event created: {created_event.get('htmlLink')}")
        return created_event
    except HttpError as err:
        logging.error(f"An error occurred creating the calendar event: {err}")
        return None

def send_email(gmail_service, to_email, subject, body):
    """Sends an email using the Gmail API."""
    try:
        message = MIMEText(body)
        message['to'] = to_email
        message['subject'] = subject
        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {'raw': encoded_message}
        
        send_message = gmail_service.users().messages().send(userId="me", body=create_message).execute()
        logging.info(f"Email sent. Message ID: {send_message['id']}")
        return send_message
    except HttpError as error:
        logging.error(f"An error occurred sending email: {error}")
        return None

# --- Temporary Test Block ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    try:
        config_path = os.path.join(SCRIPT_DIR, '..', 'config.json')
        with open(config_path, 'r') as f:
            config = json.load(f)
        google_config = config['google']
        user_config = config['user']
        email_config = config['email']
    except (FileNotFoundError, KeyError) as e:
        logging.error(f"Error loading configuration from {config_path}: {e}")
        exit()

    logging.info("--- Starting Google Services Test ---")
    gcal_service, gsheets_service, gmail_service = get_google_services()

    if gsheets_service:
        logging.info("--- Testing Google Sheets ---")
        restaurants = get_restaurants_from_sheet(gsheets_service, google_config)
        logging.info(f"Found {len(restaurants)} restaurants.")

        # Test update and delete
        if restaurants:
            test_row_index = restaurants[0]["row_index"]
            logging.info(f"--- Testing Sheet Update on row {test_row_index} ---")
            update_restaurant_in_sheet(gsheets_service, google_config, test_row_index, "12345", "TestPlatform")
            # logging.info(f"--- Testing Sheet Delete on row {test_row_index} ---")
            # delete_restaurant_from_sheet(gsheets_service, google_config, test_row_index)


    if gcal_service:
        logging.info("--- Testing Google Calendar ---")
        free_evenings = find_free_evenings(gcal_service, user_config, google_config)
        if free_evenings:
            logging.info(f"Found {len(free_evenings)} free slots:")
            for slot in free_evenings:
                logging.info(f"  - {slot.strftime('%A, %B %d at %I:%M %p %Z')}")
            
            logging.info("--- Testing Event Creation ---")
            create_calendar_event(gcal_service, free_evenings[0], "Test Restaurant", user_config['party_size'])
        else:
            logging.info("No free slots found matching your preferences.")

    if gmail_service:
        logging.info("--- Testing Gmail ---")
        send_email(gmail_service, email_config['recipient'], "Test from Reservation Agent", "This is a test email.")

    logging.info("--- Google Services Test Finished ---")