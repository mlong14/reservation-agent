import json
import random
import os
import logging
import argparse
import time
from booking_platforms import resy_client
import google_services
import datetime

# --- Constants ---
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

def setup_logging():
    """Sets up basic logging for the agent."""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def run_agent(config, gcal_service, gsheets_service, gmail_service):
    """The main orchestration logic of the reservation agent."""
    logging.info("--- Running Reservation Agent ---")
    resy_config = config['resy']
    user_config = config['user']
    google_config = config['google']
    email_config = config['email']

    # 1. Check for Existing Resy Reservations
    logging.info("Checking for existing Resy reservations...")
    try:
        active_reservations = resy_client.get_active_reservations(resy_config)
        if active_reservations:
            logging.info(f"Found {len(active_reservations)} active Resy reservation(s). Agent will not book a new one.")
            subject = "Resy Agent: Found Existing Reservation"
            body = f"The Resy agent found {len(active_reservations)} active reservation(s) and will not proceed with booking a new one.\n\nDetails:\n{json.dumps(active_reservations, indent=2)}"
            google_services.send_email(gmail_service, email_config['recipient'], subject, body)
            return
    except Exception as e:
        logging.error(f"An error occurred while checking for active reservations: {e}")
        return

    # 2. Find Availability
    logging.info("Step 1: Finding a free evening in your calendar...")
    free_evenings = google_services.find_free_evenings(gcal_service, user_config, google_config)
    if not free_evenings:
        logging.info("No free evenings found that match your preferences. Exiting.")
        return
    logging.info(f"Found {len(free_evenings)} potential evenings.")

    # 3. Get Restaurant List
    logging.info("Step 2: Getting your restaurant list from Google Sheets...")
    restaurants = google_services.get_restaurants_from_sheet(gsheets_service, google_config)
    if not restaurants:
        logging.info("No restaurants found in your sheet. Exiting.")
        return
    logging.info(f"Found {len(restaurants)} restaurants in your list.")

    # 4. Create a list of all possible combinations and shuffle it
    booking_attempts = [(evening, restaurant) for evening in free_evenings for restaurant in restaurants]
    random.shuffle(booking_attempts)

    # 5. The Main Booking Loop
    logging.info("Step 3: Searching for a reservation...")
    for evening, restaurant in booking_attempts:
        date_str = evening.strftime("%Y-%m-%d")
        if restaurant.get('platform') and restaurant['platform'].lower() == 'resy':
            logging.info(f"--- Checking for {date_str} at {restaurant['name']} ---")
            try:
                available_slots = resy_client.find_slots(
                    resy_config,
                    venue_id=int(restaurant['venue_id']),
                    party_size=user_config['party_size'],
                    date=date_str,
                    preferred_times=user_config['preferred_times'],
                    preferred_seating=user_config.get('preferred_seating', [])
                )

                if available_slots:
                    logging.info(f"  SUCCESS! Found {len(available_slots)} available slots at {restaurant['name']}.")
                    booked_successfully = False
                    for slot_to_book in available_slots[-1:]:
                        logging.info(f"  Attempting to book slot with token: {slot_to_book}")
                        
                        booking_id, slot_details = resy_client.book_slot(
                            resy_config,
                            venue_id=int(restaurant['venue_id']),
                            party_size=user_config['party_size'],
                            date=date_str,
                            preferred_times=user_config['preferred_times'],
                            preferred_seating=user_config.get('preferred_seating', []),
                        )
                        
                        if booking_id and slot_details:
                            logging.info("*** BOOKING CONFIRMED ***")
                            logging.info(f"  Confirmation ID: {booking_id}")
                            logging.info("  Creating calendar event...")
                            
                            # Get the actual reservation time from the slot details
                            reservation_time_str = slot_details.get('date', {}).get('start', ' ').split(' ')[1]
                            reservation_time = datetime.datetime.strptime(reservation_time_str, '%H:%M:%S').time()
                            reservation_datetime = evening.replace(hour=reservation_time.hour, minute=reservation_time.minute)

                            created_event = google_services.create_calendar_event(
                                gcal_service,
                                start_time=reservation_datetime,
                                restaurant_name=restaurant['name'],
                                party_size=user_config['party_size'],
                                google_config=google_config
                            )
                            logging.info("--- Agent Mission Complete ---")
                            subject = f"Resy Reservation Confirmed: {restaurant['name']} on {evening.strftime('%A, %B %d at %I:%M %p %Z')}"
                            body = f"Your reservation for {user_config['party_size']} at {restaurant['name']} on {evening.strftime('%A, %B %d at %I:%M %p %Z')} has been successfully booked.\n\nConfirmation ID: {booking_id}\nCalendar Event: {created_event.get('htmlLink')}"
                            google_services.send_email(gmail_service, email_config['recipient'], subject, body)
                            booked_successfully = True
                            return
                        else:
                            logging.warning("  Booking failed for this token. Trying next option...")
                            subject = f"Resy Reservation Failed (Token Invalid): {restaurant['name']} on {evening.strftime('%A, %B %d at %I:%M %p %Z')}"
                            body = f"Attempted to book a reservation for {user_config['party_size']} at {restaurant['name']} on {evening.strftime('%A, %B %d at %I:%M %p %Z')} with token {slot_to_book}, but the booking failed. Trying next option..."
                            google_services.send_email(gmail_service, email_config['recipient'], subject, body)
                    
                    if not booked_successfully:
                        logging.warning(f"  All available slots for {restaurant['name']} failed to book.")
                        subject = f"Resy Reservation Failed (All Slots): {restaurant['name']} on {evening.strftime('%A, %B %d at %I:%M %p %Z')}"
                        body = f"The Resy agent found available slots at {restaurant['name']} on {evening.strftime('%A, %B %d at %I:%M %p %Z')}, but all attempts to book them failed."
                        google_services.send_email(gmail_service, email_config['recipient'], subject, body)

            except Exception as e:
                logging.error(f"    An unexpected error occurred while checking {restaurant['name']}: {e}", exc_info=True)
                subject = f"Resy Agent Error: {restaurant['name']} on {evening.strftime('%A, %B %d at %I:%M %p %Z')}"
                body = f"An unexpected error occurred while trying to find or book a reservation at {restaurant['name']} on {evening.strftime('%A, %B %d at %I:%M %p %Z')}: {e}"
                google_services.send_email(gmail_service, email_config['recipient'], subject, body)

    logging.info("--- Agent Mission Finished: No reservations could be booked at this time. ---")
    subject = "Resy Agent: No Reservations Booked"
    body = "The Resy agent ran, but no reservations could be booked at this time matching your preferences."
    google_services.send_email(gmail_service, email_config['recipient'], subject, body)

def update_restaurant_list(config, gsheets_service):
    """Finds missing venue IDs and updates the Google Sheet."""
    logging.info("--- Updating Restaurant List ---")
    restaurants = google_services.get_restaurants_from_sheet(gsheets_service, config['google'])
    for restaurant in restaurants:
        if not restaurant.get('venue_id') and not restaurant.get('platform'):
            logging.info(f"Searching for venue ID for {restaurant['name']}...")
            venue_id = resy_client.find_venue_id(config['resy'], restaurant['name'])
            time.sleep(random.uniform(1, 3)) # Add a random delay
            if venue_id:
                logging.info(f"Found venue ID for {restaurant['name']}: {venue_id}")
                google_services.update_restaurant_in_sheet(gsheets_service, config['google'], restaurant['row_index'], venue_id, "Resy")
            else:
                logging.warning(f"Could not find venue ID for {restaurant['name']}. Marking as Unknown.")
                google_services.update_restaurant_in_sheet(gsheets_service, config['google'], restaurant['row_index'], "", "Unknown")

def interactive_mode(config, gcal_service, gsheets_service, gmail_service):
    """Provides an interactive menu for the user."""
    while True:
        print("\n--- Interactive Menu ---")
        print("1. Run the reservation agent")
        print("2. View upcoming Resy reservations")
        print("3. View available calendar slots")
        print("4. Update restaurant list")
        print("5. Exit")
        choice = input("Enter your choice: ")

        if choice == '1':
            run_agent(config, gcal_service, gsheets_service, gmail_service)
        elif choice == '2':
            logging.info("--- Viewing Upcoming Reservations ---")
            active_reservations = resy_client.get_active_reservations(config['resy'])
            if active_reservations:
                print(json.dumps(active_reservations, indent=2))
            else:
                print("No upcoming reservations found.")
        elif choice == '3':
            logging.info("--- Viewing Available Calendar Slots (next 60 days) ---")
            free_evenings = google_services.find_free_evenings(gcal_service, config['user'], config['google'], days_to_check=60)
            if free_evenings:
                for evening in free_evenings:
                    print(evening.strftime("%A, %B %d, %Y at %I:%M %p"))
            else:
                print("No available slots found based on your preferences.")
        elif choice == '4':
            update_restaurant_list(config, gsheets_service)
        elif choice == '5':
            break
        else:
            print("Invalid choice. Please try again.")

def main():
    """Main entry point for the script."""
    setup_logging()
    parser = argparse.ArgumentParser(description="Personal Reservation Agent")
    parser.add_argument("-i", "--interactive", action="store_true", help="Enable interactive mode")
    args = parser.parse_args()

    try:
        with open(CONFIG_PATH, 'r') as f:
            config = json.load(f)
    except (FileNotFoundError, KeyError) as e:
        logging.error(f"Error loading configuration from {CONFIG_PATH}: {e}")
        return

    logging.info("Connecting to Google Services...")
    gcal_service, gsheets_service, gmail_service = google_services.get_google_services()
    if not gcal_service or not gsheets_service or not gmail_service:
        logging.error("Failed to connect to Google Services. Exiting.")
        return

    if args.interactive:
        interactive_mode(config, gcal_service, gsheets_service, gmail_service)
    else:
        run_agent(config, gcal_service, gsheets_service, gmail_service)

if __name__ == "__main__":
    main()