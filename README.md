# Personal Reservation Agent

This is a Python-based agent that automatically finds and books restaurant reservations on Resy based on your calendar availability and preferences.

## Features

*   Finds free evenings in your Google Calendar.
*   Gets a list of your favorite restaurants from a Google Sheet.
*   Searches for available reservations on Resy.
*   Books a reservation and creates a Google Calendar event.
*   Sends an email notification upon successful booking.
*   Interactive mode to view upcoming reservations, available time slots, and update your restaurant list.

## Setup

1.  **Clone the repository:**
    ```bash
    git clone <repository-url>
    cd reservation-agent
    ```

2.  **Create a virtual environment and install dependencies:**
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    ```

3.  **Configure the agent:**
    *   Rename `config.json.example` to `config.json` and fill in the required values.
    *   `resy`: Your Resy API key and auth token.
    *   `user`: Your reservation preferences (party size, preferred days, and times).
    *   `google`: Your Google Sheet ID and calendar IDs.
    *   `email`: Your email address for notifications.
    *   Create a `credentials.json` file for the Google API. Follow the instructions [here](https://developers.google.com/workspace/guides/create-credentials) to create your credentials.

## Usage

### Non-Interactive Mode

To run the agent once, simply execute the `agent.py` script:

```bash
python agent.py
```

### Interactive Mode

To use the interactive menu, run the script with the `--interactive` flag:

```bash
python agent.py --interactive
```

The interactive menu provides the following options:

*   **Run the reservation agent:** Runs the agent to find and book a reservation.
*   **View upcoming Resy reservations:** Displays a list of your upcoming reservations on Resy.
*   **View available calendar slots:** Shows your available time slots for the next 60 days based on your preferences.
*   **Update restaurant list:** Updates your restaurant list in the Google Sheet by finding missing Resy venue IDs.
*   **Exit:** Exits the interactive menu.
