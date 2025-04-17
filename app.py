import sys
import requests
import json
import re # Import regex for validation
import os # Added for environment variables
from dotenv import load_dotenv # Added
from datetime import date, timedelta, datetime
import calendar
import atexit # To shut down scheduler
import logging # For scheduler logging

from flask import Flask, render_template, request, flash, jsonify, redirect, url_for # Added redirect, url_for
from flask_mail import Mail, Message # Added Mail, Message
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv() # Load environment variables from .env file

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'default-fallback-secret-key') # Use env var for secret key

# --- Flask-Mail Configuration ---
# IMPORTANT: Set these as environment variables for security!
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER') # e.g., 'smtp.gmail.com'
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587)) # e.g., 587 (TLS) or 465 (SSL)
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true'
app.config['MAIL_USE_SSL'] = os.environ.get('MAIL_USE_SSL', 'false').lower() == 'true'
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME') # Your email address
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD') # Your email password or App Password
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', app.config['MAIL_USERNAME']) # Usually same as username
# --- Recipient Configuration ---
# IMPORTANT: Set this as an environment variable!
MAIL_RECIPIENT = os.environ.get('MAIL_RECIPIENT') # Email address to send notifications to

mail = Mail(app)

# --- Notification Config File ---
CONFIG_FILE = 'notification_config.json'

def load_notification_settings():
    """Loads notification settings from JSON file."""
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # Return defaults if file not found or invalid
        today = date.today()
        default_month = (today.replace(day=1) + timedelta(days=32)).strftime('%Y-%m')
        return {
            'outbound_month': default_month,
            'duration_from': '2',
            'duration_to': '7'
        }

def save_notification_settings(settings):
    """Saves notification settings to JSON file."""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(settings, f, indent=4)
        return True
    except IOError as e:
        print(f"Error saving notification settings: {e}")
        return False

# --- Global Store for Background Task Results ---
# CAUTION: This is a simple in-memory store. Data is lost on app restart.
# For persistence, use a file or database. Also consider thread safety for complex updates.
background_deal_findings = {
    "last_checked": None,
    "deals_under_25": [], # List of trip_details dicts
    "notified_deals": set() # Set of unique identifiers (e.g., "DEST-PRICE-OUT_DATE")
}

# --- Define API Endpoint Templates ---
# Moved ONE_WAY_MONTH_API_TEMPLATE here and renamed API_ENDPOINT_TEMPLATE
ROUND_TRIP_API_TEMPLATE = "https://www.ryanair.com/api/farfnd/v4/roundTripFares?departureAirportIataCode={origin_iata}&market=en-gb&adultPaxCount=1&arrivalAirportIataCode={destination_iata}&searchMode=ALL&outboundDepartureDateFrom={out_date_from}&outboundDepartureDateTo={out_date_to}&inboundDepartureDateFrom={in_date_from}&inboundDepartureDateTo={in_date_to}&durationFrom={duration_from}&durationTo={duration_to}&currency={currency}"
ONE_WAY_MONTH_API_TEMPLATE = "https://www.ryanair.com/api/farfnd/v4/oneWayFares/{origin_iata}/{destination_iata}/cheapestPerDay?outboundMonthOfDate={month_date}&currency={currency}"
# -----------------------------------

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36'
}

def get_last_day_of_month(year, month):
    return calendar.monthrange(year, month)[1]

# === Single Round Trip Search Routes ===

@app.route('/')
def index():
    today = date.today()
    default_month = (today.replace(day=1) + timedelta(days=32)).strftime('%Y-%m')
    return render_template('index.html', default_month=default_month)

@app.route('/search', methods=['POST'])
def search_flights():
    # Changed variable names to reflect IATA code input
    origin_iata = request.form.get('origin_iata', '').strip().upper()
    destination_iata = request.form.get('destination_iata', '').strip().upper()
    outbound_month_str = request.form.get('outbound_month')
    duration_from = request.form.get('duration_from', '2')
    duration_to = request.form.get('duration_to', '7')
    currency = "EUR"

    # --- Input Validation (IATA Code) ---
    iata_pattern = re.compile(r"^[A-Za-z]{3}$") # Simple 3-letter validation
    if not (origin_iata and destination_iata and outbound_month_str):
        flash("Please fill in Origin IATA, Destination IATA, and Outbound Month.")
        # Pass back entered values
        return render_template('index.html', default_month=outbound_month_str or date.today().strftime('%Y-%m'),
                               origin_iata=origin_iata, destination_iata=destination_iata)

    if not iata_pattern.match(origin_iata) or not iata_pattern.match(destination_iata):
         flash("Origin and Destination must be 3-letter IATA codes.")
         return render_template('index.html', default_month=outbound_month_str,
                                origin_iata=origin_iata, destination_iata=destination_iata)

    print(f"Round Trip Search: {origin_iata} -> {destination_iata}")

    # --- Date Parsing and Validation ---
    try:
        year, month = map(int, outbound_month_str.split('-'))
        out_date_from = date(year, month, 1)
        last_day = get_last_day_of_month(year, month)
        out_date_to = date(year, month, last_day)
        in_date_from = out_date_from
        next_month_date = (out_date_to.replace(day=1) + timedelta(days=32))
        in_year, in_month = next_month_date.year, next_month_date.month
        in_last_day = get_last_day_of_month(in_year, in_month)
        in_date_to = date(in_year, in_month, in_last_day)
    except ValueError:
        flash("Invalid month format. Please use YYYY-MM.")
        return render_template('index.html', default_month=date.today().strftime('%Y-%m'),
                               origin_iata=origin_iata, destination_iata=destination_iata)

    # --- Construct API URL (using form IATA codes) ---
    api_url = ROUND_TRIP_API_TEMPLATE.format(
        origin_iata=origin_iata,
        destination_iata=destination_iata,
        out_date_from=out_date_from.strftime("%Y-%m-%d"),
        out_date_to=out_date_to.strftime("%Y-%m-%d"),
        in_date_from=in_date_from.strftime("%Y-%m-%d"),
        in_date_to=in_date_to.strftime("%Y-%m-%d"),
        duration_from=duration_from,
        duration_to=duration_to,
        currency=currency
    )

    print(f"Calling API: {api_url}")

    # --- Call API and Process Results (logic remains largely the same) ---
    all_trips = []
    error_message = None
    try:
        response = requests.get(api_url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        data = response.json()
        if 'fares' in data and data['fares']:
            for fare in data['fares']:
                try:
                    total_price = fare['summary']['price']['value']
                    trip_details = {
                        'origin': fare.get('outbound', {}).get('departureAirport', {}).get('iataCode'),
                        'destination': fare.get('outbound', {}).get('arrivalAirport', {}).get('iataCode'),
                        'total_price': total_price,
                        'currency': fare.get('summary', {}).get('price', {}).get('currencyCode'),
                        'outbound': {
                            'flight_no': fare.get('outbound', {}).get('flightNumber'),
                            'dep_time': fare.get('outbound', {}).get('departureDate'),
                            'arr_time': fare.get('outbound', {}).get('arrivalDate'),
                            'price': fare.get('outbound', {}).get('price', {}).get('value')
                        },
                        'inbound': {
                            'flight_no': fare.get('inbound', {}).get('flightNumber'),
                            'dep_time': fare.get('inbound', {}).get('departureDate'),
                            'arr_time': fare.get('inbound', {}).get('arrivalDate'),
                            'price': fare.get('inbound', {}).get('price', {}).get('value')
                        }
                    }
                    all_trips.append(trip_details)
                except (KeyError, TypeError) as e:
                    print(f"Warning: Could not parse fare details, skipping. Error: {e}. Fare: {fare}")
                    continue
            all_trips.sort(key=lambda x: x['total_price'])
            if not all_trips and not error_message:
                 error_message = "Found flight data, but couldn't parse prices correctly for any trip."
        else:
            error_message = f"No round trips found matching your criteria for {origin_iata} -> {destination_iata} in {outbound_month_str}."

    except requests.exceptions.HTTPError as http_err:
        print(f"HTTP error: {http_err} - Status: {http_err.response.status_code}")
        error_message = f"API Error ({http_err.response.status_code}) for {origin_iata} -> {destination_iata}. Check IATA codes or try later."
    except requests.exceptions.RequestException as req_err:
        print(f"Request error: {req_err}")
        error_message = "Network Error: Could not connect to Ryanair API."
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"Parsing error: {e}")
        error_message = "API Error: Received unexpected data format from Ryanair."
    except Exception as e:
        print(f"Unexpected error: {e}")
        error_message = "An unexpected error occurred."

    if error_message:
        flash(error_message)

    top_10_trips = all_trips[:10]
    # Pass IATA codes back instead of city names
    return render_template('results.html', top_trips=top_10_trips, query=request.form,
                           origin_iata=origin_iata, destination_iata=destination_iata)

# === Multi-City Cheapest Round Trip Search Routes ===

@app.route('/multi_round_trip', methods=['GET'])
def multi_round_trip_form():
    today = date.today()
    default_month = (today.replace(day=1) + timedelta(days=32)).strftime('%Y-%m')
    return render_template('multi_round_trip_search.html', default_month=default_month, duration_from=2, duration_to=7)

@app.route('/multi_round_trip', methods=['POST'])
def process_multi_round_trip():
    # Get raw text area content
    origin_iatas_raw = request.form.get('origin_iatas', '')
    destination_iatas_raw = request.form.get('destination_iatas', '')
    search_month_str = request.form.get('outbound_month')
    duration_from = request.form.get('duration_from', '2')
    duration_to = request.form.get('duration_to', '7')
    currency = "EUR"

    # Parse IATA lists (split by newline, strip whitespace, uppercase, remove empty)
    origin_iatas = [code.strip().upper() for code in origin_iatas_raw.splitlines() if code.strip()]
    destination_iatas = [code.strip().upper() for code in destination_iatas_raw.splitlines() if code.strip()]

    # --- Input Validation ---
    if not (origin_iatas and destination_iatas and search_month_str and duration_from and duration_to):
        flash("Please provide Origin(s), Destination(s), Month, and Durations.")
        return render_template('multi_round_trip_search.html',
                               default_month=search_month_str or date.today().strftime('%Y-%m'),
                               origin_iatas=origin_iatas_raw,
                               destination_iatas=destination_iatas_raw,
                               duration_from=duration_from or 2,
                               duration_to=duration_to or 7)

    # Validate each IATA code format
    iata_pattern = re.compile(r"^[A-Za-z]{3}$")
    invalid_origins = [code for code in origin_iatas if not iata_pattern.match(code)]
    invalid_destinations = [code for code in destination_iatas if not iata_pattern.match(code)]

    error_found = False
    if invalid_origins:
        flash(f"Invalid Origin IATA code(s) found: {', '.join(invalid_origins)}. Please use 3 letters only.")
        error_found = True
    if invalid_destinations:
        flash(f"Invalid Destination IATA code(s) found: {', '.join(invalid_destinations)}. Please use 3 letters only.")
        error_found = True

    # Validate duration
    try:
        int_duration_from = int(duration_from)
        int_duration_to = int(duration_to)
        if int_duration_from < 1 or int_duration_to < 1 or int_duration_from > int_duration_to:
             raise ValueError("Invalid duration range.")
    except ValueError:
        flash("Invalid duration. Please enter positive numbers, with 'Min Duration' less than or equal to 'Max Duration'.")
        error_found = True

    if error_found:
         return render_template('multi_round_trip_search.html',
                                default_month=search_month_str or date.today().strftime('%Y-%m'),
                                origin_iatas=origin_iatas_raw,
                                destination_iatas=destination_iatas_raw,
                                duration_from=duration_from or 2,
                                duration_to=duration_to or 7)

    # --- Date Calculation (similar to single round trip) ---
    try:
        year, month = map(int, search_month_str.split('-'))
        out_date_from = date(year, month, 1)
        last_day = get_last_day_of_month(year, month)
        out_date_to = date(year, month, last_day)
        in_date_from = out_date_from # Inbound can start same day as outbound starts
        # Calculate end of inbound search window (end of next month)
        next_month_date = (out_date_to.replace(day=1) + timedelta(days=32))
        in_year, in_month = next_month_date.year, next_month_date.month
        in_last_day = get_last_day_of_month(in_year, in_month)
        in_date_to = date(in_year, in_month, in_last_day)
    except ValueError:
        flash("Invalid month format. Please use YYYY-MM.")
        return render_template('multi_round_trip_search.html',
                                default_month=date.today().strftime('%Y-%m'),
                                origin_iatas=origin_iatas_raw,
                                destination_iatas=destination_iatas_raw,
                                duration_from=duration_from or 2,
                                duration_to=duration_to or 7)

    # --- Search Loop ---
    overall_cheapest_trip = {
        "total_price": sys.float_info.max,
        "origin_iata": None,
        "destination_iata": None,
        "currency": currency,
        "outbound_dep_time": None,
        "outbound_arr_time": None,
        "inbound_dep_time": None,
        "inbound_arr_time": None
        # Add other details if needed, like flight numbers
    }
    errors = []

    total_pairs = len(origin_iatas) * len(destination_iatas)
    print(f"Starting Multi-City Round Trip Search for {total_pairs} pairs in {search_month_str}...")

    for origin_iata in origin_iatas:
        for destination_iata in destination_iatas:
            if origin_iata == destination_iata:
                continue

            print(f"  Checking: {origin_iata} <-> {destination_iata}")

            # Construct API URL using ROUND_TRIP_API_TEMPLATE
            api_url = ROUND_TRIP_API_TEMPLATE.format(
                origin_iata=origin_iata,
                destination_iata=destination_iata,
                out_date_from=out_date_from.strftime("%Y-%m-%d"),
                out_date_to=out_date_to.strftime("%Y-%m-%d"),
                in_date_from=in_date_from.strftime("%Y-%m-%d"),
                in_date_to=in_date_to.strftime("%Y-%m-%d"),
                duration_from=duration_from,
                duration_to=duration_to,
                currency=currency
            )

            try:
                response = requests.get(api_url, headers=HEADERS, timeout=30) # Increased timeout slightly
                response.raise_for_status()
                data = response.json()

                # Parse round trip response
                if 'fares' in data and data['fares']:
                    for fare in data['fares']:
                        try:
                            current_total_price = fare.get('summary', {}).get('price', {}).get('value')
                            if current_total_price is not None and current_total_price < overall_cheapest_trip['total_price']:
                                overall_cheapest_trip['total_price'] = current_total_price
                                overall_cheapest_trip['origin_iata'] = fare.get('outbound', {}).get('departureAirport', {}).get('iataCode')
                                overall_cheapest_trip['destination_iata'] = fare.get('outbound', {}).get('arrivalAirport', {}).get('iataCode')
                                overall_cheapest_trip['currency'] = fare.get('summary', {}).get('price', {}).get('currencyCode')
                                overall_cheapest_trip['outbound_dep_time'] = fare.get('outbound', {}).get('departureDate')
                                overall_cheapest_trip['outbound_arr_time'] = fare.get('outbound', {}).get('arrivalDate')
                                overall_cheapest_trip['inbound_dep_time'] = fare.get('inbound', {}).get('departureDate')
                                overall_cheapest_trip['inbound_arr_time'] = fare.get('inbound', {}).get('arrivalDate')
                                # Optionally add flight numbers etc.
                        except (KeyError, TypeError) as e:
                             print(f"    Warning: Parsing error for fare {origin_iata}<->{destination_iata}: {e}. Fare: {fare}")
                             continue # Skip this specific fare if parsing fails
            except requests.exceptions.HTTPError as http_err:
                 print(f"    HTTP error for {origin_iata}<->{destination_iata}: {http_err} - Status: {http_err.response.status_code}")
                 # Optionally add specific error message to errors list
                 # errors.append(f"API Error ({http_err.response.status_code}) for {origin_iata}<->{destination_iata}")
            except requests.exceptions.RequestException as req_err:
                errors.append(f"Network Error connecting to API for {origin_iata}<->{destination_iata}: {req_err}")
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                errors.append(f"API Data Error for {origin_iata}<->{destination_iata}: {e}")
            except Exception as e:
                errors.append(f"Unexpected error searching {origin_iata}<->{destination_iata}: {e}")

    # --- Display Results ---
    print(f"Multi-City Round Trip Search complete. Found cheapest price: {overall_cheapest_trip['total_price']}")
    for error in errors:
        flash(error) # Show accumulated errors

    if overall_cheapest_trip["total_price"] == sys.float_info.max:
        flash("No round trips found for any of the specified IATA combinations, month, and duration.")
        cheapest_flight_result = None
    else:
        # Format dates for better readability if needed
        # Example: overall_cheapest_trip['outbound_dep_time'] = format_datetime(overall_cheapest_trip['outbound_dep_time'])
        cheapest_flight_result = overall_cheapest_trip

    # Pass raw form data back for repopulation if needed
    return render_template('multi_round_trip_results.html',
                           cheapest_flight=cheapest_flight_result,
                           origin_iatas_raw=origin_iatas_raw,
                           destination_iatas_raw=destination_iatas_raw,
                           outbound_month=search_month_str,
                           duration_from=duration_from,
                           duration_to=duration_to)

# === Sofia Top 10 Deals ===

SOFIA_DESTINATIONS = [
    "ALC", "BCN", "MAD", "AGP", "VLC",
    "BRI", "BGY", "BLQ", "CTA", "NAP", "CIA", "TSF",
    "BER", "CGN", "FKB", "FMM", "NUE",
    "BHX", "BRS", "EDI", "LPL", "STN",
    "CHQ", "CFU", "RHO", "JSI",
    "BTS", "CRL", "BUD", "CPH", "DUB", "EIN", "MLA", "PFO", "BVA", "POZ", "VIE", "WRO", "ZAD"
]

@app.route('/sofia_deals')
def sofia_deals():
    # Load current notification settings to populate the form defaults
    current_notification_settings = load_notification_settings()

    # Get parameters from URL (if submitted) or use notification settings as defaults for the form
    search_month_str = request.args.get('outbound_month', current_notification_settings.get('outbound_month'))
    duration_from = request.args.get('duration_from', current_notification_settings.get('duration_from'))
    duration_to = request.args.get('duration_to', current_notification_settings.get('duration_to'))

    cheapest_trips_list = []
    errors = []

    # Only perform search if the form was actually submitted (check for presence of args)
    if 'outbound_month' in request.args and 'duration_from' in request.args and 'duration_to' in request.args:
        print(f"Starting Sofia Deals MANUAL search for {search_month_str} ({duration_from}-{duration_to} days)...")
        # --- Validate and Calculate Dates for Manual Search ---
        try:
            year, month = map(int, search_month_str.split('-'))
            out_date_from = date(year, month, 1)
            last_day = get_last_day_of_month(year, month)
            out_date_to = date(year, month, last_day)
            in_date_from = out_date_from
            next_month_date = (out_date_to.replace(day=1) + timedelta(days=32))
            in_year_in, in_month_in = next_month_date.year, next_month_date.month
            in_last_day = get_last_day_of_month(in_year_in, in_month_in)
            in_date_to = date(in_year_in, in_month_in, in_last_day)
            # Basic duration validation
            int_dur_from = int(duration_from)
            int_dur_to = int(duration_to)
            if int_dur_from < 1 or int_dur_to < 1 or int_dur_from > int_dur_to:
                raise ValueError("Invalid duration range")

        except Exception as e:
            flash(f"Invalid date/duration parameters: {e}. Please check values.", "error")
            # Pass back parameters used so form is repopulated
            return render_template('sofia_deals.html',
                                   top_trips=[],
                                   search_month=search_month_str,
                                   duration_from=duration_from,
                                   duration_to=duration_to,
                                   errors=[f"Invalid date/duration parameters: {e}"])

        # --- Manual Search Logic --- (Moved inside the conditional block)
        cheapest_per_destination = {}
        origin_iata = "SOF"
        currency = "EUR"

        for destination_iata in SOFIA_DESTINATIONS:
            print(f"  Checking SOF -> {destination_iata}")
            api_url = ROUND_TRIP_API_TEMPLATE.format(
                origin_iata=origin_iata,
                destination_iata=destination_iata,
                out_date_from=out_date_from.strftime("%Y-%m-%d"),
                out_date_to=out_date_to.strftime("%Y-%m-%d"),
                in_date_from=in_date_from.strftime("%Y-%m-%d"),
                in_date_to=in_date_to.strftime("%Y-%m-%d"),
                duration_from=duration_from,
                duration_to=duration_to,
                currency=currency
            )
            try:
                response = requests.get(api_url, headers=HEADERS, timeout=30)
                response.raise_for_status()
                data = response.json()

                if 'fares' in data and data['fares']:
                    for fare in data['fares']:
                        try:
                            total_price = fare.get('summary', {}).get('price', {}).get('value')
                            if total_price is not None:
                                trip_details = {
                                    'origin_iata': fare.get('outbound', {}).get('departureAirport', {}).get('iataCode'),
                                    'destination_iata': fare.get('outbound', {}).get('arrivalAirport', {}).get('iataCode'),
                                    'total_price': total_price,
                                    'currency': fare.get('summary', {}).get('price', {}).get('currencyCode'),
                                    'outbound_dep_time': fare.get('outbound', {}).get('departureDate'),
                                    'outbound_arr_time': fare.get('outbound', {}).get('arrivalDate'),
                                    'inbound_dep_time': fare.get('inbound', {}).get('departureDate'),
                                    'inbound_arr_time': fare.get('inbound', {}).get('arrivalDate')
                                }
                                if destination_iata not in cheapest_per_destination or trip_details['total_price'] < cheapest_per_destination[destination_iata]['total_price']:
                                    cheapest_per_destination[destination_iata] = trip_details
                                break # Assume first is cheapest
                        except (KeyError, TypeError) as e:
                            print(f"    Warning: Parsing error for manual fare SOF->{destination_iata}: {e}. Fare: {fare}")
                            continue
            except requests.exceptions.HTTPError as http_err:
                print(f"    HTTP error for SOF->{destination_iata} (Manual): {http_err} - Status: {http_err.response.status_code}")
                errors.append(f"API Error ({http_err.response.status_code}) for {destination_iata}")
            except requests.exceptions.RequestException as req_err:
                print(f"    Request error for SOF->{destination_iata} (Manual): {req_err}")
                errors.append(f"Network Error for {destination_iata}")
            except Exception as e:
                print(f"    Unexpected error for SOF->{destination_iata} (Manual): {e}")
                errors.append(f"Unexpected error for {destination_iata}")

        # Convert results to list and sort
        cheapest_trips_list = list(cheapest_per_destination.values())
        cheapest_trips_list.sort(key=lambda x: x['total_price'])
        print(f"Sofia Deals page loaded. Found {len(cheapest_trips_list)} destinations via manual search.")

        # Flash errors specific to this search
        for error in errors:
            flash(error, "error")
        if not cheapest_trips_list and not errors:
            flash("Could not find any round trips from Sofia for the specified destinations and period.", "warning")
    else:
        print("Sofia Deals page loaded initially (no search performed).")
        # Optionally add a message indicating that the user needs to submit the form
        flash("Select month and duration, then click 'Update Deals' to search.", "info")

    # Pass results (possibly empty) and form values to template
    return render_template('sofia_deals.html',
                           top_trips=cheapest_trips_list,
                           search_month=search_month_str,
                           duration_from=duration_from,
                           duration_to=duration_to)

# === Notification Configuration Route ===
@app.route('/configure_notifications', methods=['GET', 'POST'])
def configure_notifications():
    if request.method == 'POST':
        search_month_str = request.form.get('outbound_month')
        duration_from = request.form.get('duration_from')
        duration_to = request.form.get('duration_to')

        # Basic validation (can be enhanced)
        valid = True
        try:
            year, month = map(int, search_month_str.split('-'))
            if not (1 <= month <= 12):
                 raise ValueError("Invalid month")
            int_duration_from = int(duration_from)
            int_duration_to = int(duration_to)
            if int_duration_from < 1 or int_duration_to < 1 or int_duration_from > int_duration_to:
                 raise ValueError("Invalid duration range")
        except (ValueError, TypeError, AttributeError):
            valid = False
            flash("Invalid input. Please check month and duration values.", "error")

        if valid:
            settings = {
                'outbound_month': search_month_str,
                'duration_from': duration_from,
                'duration_to': duration_to
            }
            if save_notification_settings(settings):
                flash("Notification settings saved successfully! The background checker will use these parameters.", "success")
            else:
                flash("Error saving notification settings. Please check server logs.", "error")
            return redirect(url_for('configure_notifications')) # Redirect to refresh page
        else:
            # If validation fails, reload form with submitted values
            current_settings = {
                 'outbound_month': search_month_str,
                 'duration_from': duration_from,
                 'duration_to': duration_to
            }
            return render_template('configure_notifications.html', current_settings=current_settings)

    # GET request: Load current settings and display the form
    current_settings = load_notification_settings()
    return render_template('configure_notifications.html', current_settings=current_settings)

# === Test Email Route ===
@app.route('/test_email')
def test_email():
    subject = "Test Email from Ryanair Deals App"
    body = f"This is a test email sent at {datetime.now()} to confirm your email configuration is working."
    recipient = MAIL_RECIPIENT # Get recipient from env var

    if not recipient:
        flash("MAIL_RECIPIENT environment variable is not set.", "error")
        return "Recipient email not configured.", 500

    msg = Message(subject, recipients=[recipient])
    msg.body = body

    try:
        mail.send(msg)
        flash(f"Test email successfully sent to {recipient}!", "success")
        print(f"Test email successfully sent to {recipient}")
    except Exception as e:
        flash(f"ERROR sending test email: {e}", "error")
        print(f"ERROR sending test email: {e}")
        # Return a more informative error page if needed
        return f"Error sending email: {e}", 500

    return f"Attempted to send test email to {recipient}. Check inbox and console log."

# === Background Task for Finding Deals ===

NOTIFICATION_THRESHOLD = 30.0

def check_sofia_deals_background():
    """Scheduled task to check Sofia deals based on saved config."""
    print(f"\n[{datetime.now()}] Running background check for Sofia deals...")

    # Load configured settings
    config = load_notification_settings()
    search_month_str = config.get('outbound_month')
    duration_from = config.get('duration_from', '2') # Default if missing
    duration_to = config.get('duration_to', '7')   # Default if missing
    origin_iata = "SOF"
    currency = "EUR"

    # Validate loaded settings before proceeding
    try:
        year, month = map(int, search_month_str.split('-'))
        out_date_from = date(year, month, 1)
        last_day = get_last_day_of_month(year, month)
        out_date_to = date(year, month, last_day)
        in_date_from = out_date_from
        next_month_date = (out_date_to.replace(day=1) + timedelta(days=32))
        in_year, in_month = next_month_date.year, next_month_date.month
        in_last_day = get_last_day_of_month(in_year, in_month)
        in_date_to = date(in_year, in_month, in_last_day)
        # Validate duration format from config
        int(duration_from)
        int(duration_to)
        print(f"  Using configured settings: Month={search_month_str}, Duration={duration_from}-{duration_to}")
    except (AttributeError, ValueError, TypeError) as e:
        print(f"  ERROR: Invalid or missing notification configuration in '{CONFIG_FILE}'. Skipping background check. Error: {e}")
        return # Stop the task if config is bad

    found_deals_under_threshold = []
    for destination_iata in SOFIA_DESTINATIONS:
        api_url = ROUND_TRIP_API_TEMPLATE.format(
            origin_iata=origin_iata, destination_iata=destination_iata,
            out_date_from=out_date_from.strftime("%Y-%m-%d"), out_date_to=out_date_to.strftime("%Y-%m-%d"),
            in_date_from=in_date_from.strftime("%Y-%m-%d"), in_date_to=in_date_to.strftime("%Y-%m-%d"),
            duration_from=duration_from, duration_to=duration_to, currency=currency
        )
        try:
            response = requests.get(api_url, headers=HEADERS, timeout=30)
            response.raise_for_status()
            data = response.json()
            if 'fares' in data and data['fares']:
                for fare in data['fares']:
                    try:
                        total_price = fare.get('summary', {}).get('price', {}).get('value')
                        if total_price is not None and total_price < NOTIFICATION_THRESHOLD:
                            trip_details = {
                                'origin_iata': fare.get('outbound', {}).get('departureAirport', {}).get('iataCode'),
                                'destination_iata': fare.get('outbound', {}).get('arrivalAirport', {}).get('iataCode'),
                                'total_price': total_price,
                                'currency': fare.get('summary', {}).get('price', {}).get('currencyCode'),
                                'outbound_dep_time': fare.get('outbound', {}).get('departureDate'),
                                'inbound_dep_time': fare.get('inbound', {}).get('departureDate')
                            }
                            found_deals_under_threshold.append(trip_details)
                            break
                    except (KeyError, TypeError):
                        continue
        except Exception as e:
            print(f"    Error checking {destination_iata} in background: {e}")
            continue

    # Update global store
    global background_deal_findings
    background_deal_findings["last_checked"] = datetime.now()
    background_deal_findings["deals_under_25"] = found_deals_under_threshold
    print(f"[{datetime.now()}] Background check finished. Found {len(found_deals_under_threshold)} deals under {NOTIFICATION_THRESHOLD} EUR.")

    # --- Email Notification Logic --- 
    newly_found_deals_for_email = []
    if found_deals_under_threshold:
        for deal in found_deals_under_threshold:
            deal_id = f"{deal['destination_iata']}-{deal['total_price']}-{deal['outbound_dep_time']}"
            if deal_id not in background_deal_findings["notified_deals"]:
                newly_found_deals_for_email.append(deal)
                # Add to notified set *after* successful send attempt below

    if newly_found_deals_for_email:
        print(f"Found {len(newly_found_deals_for_email)} new deals under {NOTIFICATION_THRESHOLD} EUR to notify via email.")
        if not MAIL_RECIPIENT:
            print("  ERROR: MAIL_RECIPIENT environment variable not set. Cannot send email.")
            return # Exit function if no recipient is configured

        subject = f"Ryanair Deal Alert! {len(newly_found_deals_for_email)} new flight(s) under {NOTIFICATION_THRESHOLD} EUR from SOF"
        body_lines = [f"Found {len(newly_found_deals_for_email)} new round trip deal(s) from Sofia (SOF) under {NOTIFICATION_THRESHOLD} EUR:", ""]
        for deal in newly_found_deals_for_email:
            body_lines.append(f"- {deal['destination_iata']} for {deal['total_price']}{deal['currency']} (Outbound: {deal['outbound_dep_time'][:10]}, Inbound: {deal['inbound_dep_time'][:10]})")
        body = "\n".join(body_lines)

        # Move Message creation and sending inside app_context
        try:
            with app.app_context():
                msg = Message(subject, recipients=[MAIL_RECIPIENT])
                msg.body = body
                mail.send(msg)
            print(f"  Successfully sent email notification to {MAIL_RECIPIENT}")
            # Update notified set only after successful send attempt
            for deal in newly_found_deals_for_email:
                 deal_id = f"{deal['destination_iata']}-{deal['total_price']}-{deal['outbound_dep_time']}"
                 background_deal_findings["notified_deals"].add(deal_id)
        except Exception as e:
            print(f"  ERROR sending email notification: {e}")
            # Decide if you want to retry or just skip marking as notified on failure

# --- Initialize Scheduler ---
logging.basicConfig()
logging.getLogger('apscheduler').setLevel(logging.WARNING) # Reduce APScheduler noise

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(check_sofia_deals_background, 'interval', minutes=2)
try:
    scheduler.start()
    print("Background deal checker scheduled to run every 2 minutes.")
    # Shut down the scheduler when exiting the app
    atexit.register(lambda: scheduler.shutdown())
except (KeyboardInterrupt, SystemExit):
    scheduler.shutdown()

# === Main Execution ===
if __name__ == '__main__':
    # Note: Flask's default reloader might interfere with APScheduler.
    # Consider running with app.run(debug=True, use_reloader=False) if issues arise during development.
    app.run(debug=True, use_reloader=False) 