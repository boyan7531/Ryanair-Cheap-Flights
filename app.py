import sys
import requests
import json
import re # Import regex for validation
import os # Added for environment variables
from datetime import date, timedelta, datetime
import calendar
import atexit # To shut down scheduler
import logging # For scheduler logging
import uuid # Added for generating unique IDs
from supabase import create_client, Client # Added for Supabase

from flask import Flask, render_template, request, flash, jsonify, redirect, url_for # Added redirect, url_for
from flask_mail import Mail, Message # Added Mail, Message
from apscheduler.schedulers.background import BackgroundScheduler
# Test comment
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
NOTIFICATION_RULES_FILE = 'notification_rules.json' # Renamed file

# --- Supabase Setup ---
supabase_url: str = os.environ.get("SUPABASE_URL")
supabase_key: str = os.environ.get("SUPABASE_ANON_KEY")
supabase: Client = None
if supabase_url and supabase_key:
    try:
        supabase = create_client(supabase_url, supabase_key)
        print("Supabase client initialized successfully.")
    except Exception as e:
        print(f"Error initializing Supabase client: {e}")
else:
    print("Warning: SUPABASE_URL or SUPABASE_ANON_KEY environment variables not set. Supabase integration disabled.")
# ---------------------

def load_notification_rules(): # Renamed function
    """Loads the list of notification rules from JSON file."""
    try:
        with open(NOTIFICATION_RULES_FILE, 'r') as f:
            rules = json.load(f)
            if isinstance(rules, list):
                return rules
            else:
                print(f"Warning: Content of {NOTIFICATION_RULES_FILE} is not a list. Returning empty list.")
                return [] # Return empty list if format is wrong
    except (FileNotFoundError, json.JSONDecodeError):
        return [] # Return empty list if file not found or invalid

def save_notification_rules(rules): # Renamed function
    """Saves the list of notification rules to JSON file."""
    if not isinstance(rules, list):
        print("Error: Attempted to save non-list data as notification rules.")
        return False
    try:
        with open(NOTIFICATION_RULES_FILE, 'w') as f:
            json.dump(rules, f, indent=4)
        return True
    except IOError as e:
        print(f"Error saving notification rules: {e}")
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
    # Pass 'now' to the template for the footer
    return render_template('index.html', default_month=default_month, now=datetime.utcnow())

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
        # Pass back entered values and 'now'
        return render_template('index.html', default_month=outbound_month_str or date.today().strftime('%Y-%m'),
                               origin_iata=origin_iata, destination_iata=destination_iata, now=datetime.utcnow())

    if not iata_pattern.match(origin_iata) or not iata_pattern.match(destination_iata):
         flash("Origin and Destination must be 3-letter IATA codes.")
         # Pass back entered values and 'now'
         return render_template('index.html', default_month=outbound_month_str,
                                origin_iata=origin_iata, destination_iata=destination_iata, now=datetime.utcnow())

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
        # Pass back entered values and 'now'
        return render_template('index.html', default_month=date.today().strftime('%Y-%m'),
                               origin_iata=origin_iata, destination_iata=destination_iata, now=datetime.utcnow())

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
    # Pass IATA codes back instead of city names and add 'now' for base template
    return render_template('results.html', top_trips=top_10_trips, query=request.form,
                           origin_iata=origin_iata, destination_iata=destination_iata, now=datetime.utcnow())

# === Multi-City Cheapest Round Trip Search Routes ===

@app.route('/multi_round_trip', methods=['GET'])
def multi_round_trip_form():
    today = date.today()
    default_month = (today.replace(day=1) + timedelta(days=32)).strftime('%Y-%m')
    return render_template('multi_round_trip_search.html', default_month=default_month, duration_from=2, duration_to=7, now=datetime.utcnow())

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
                               duration_to=duration_to or 7,
                               now=datetime.utcnow())

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
                                duration_to=duration_to or 7,
                                now=datetime.utcnow())

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
                                duration_to=duration_to or 7,
                                now=datetime.utcnow())

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

    # Pass raw form data back for repopulation if needed and add 'now'
    return render_template('multi_round_trip_results.html',
                           cheapest_flight=cheapest_flight_result,
                           origin_iatas_raw=origin_iatas_raw,
                           destination_iatas_raw=destination_iatas_raw,
                           outbound_month=search_month_str,
                           duration_from=duration_from,
                           duration_to=duration_to,
                           now=datetime.utcnow())

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
    # Define explicit defaults for the form
    today = date.today()
    default_month = (today.replace(day=1) + timedelta(days=32)).strftime('%Y-%m')
    default_duration_from = "2"
    default_duration_to = "7"

    # Get parameters from URL (if submitted) or use the explicit defaults
    search_month_str = request.args.get('outbound_month', default_month)
    duration_from = request.args.get('duration_from', default_duration_from)
    duration_to = request.args.get('duration_to', default_duration_to)

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
            # Pass back parameters used so form is repopulated and 'now'
            return render_template('sofia_deals.html',
                                   top_trips=[],
                                   search_month=search_month_str,
                                   duration_from=duration_from,
                                   duration_to=duration_to,
                                   errors=[f"Invalid date/duration parameters: {e}"],
                                   now=datetime.utcnow())

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

    # Pass results (possibly empty), form values and 'now' to template
    return render_template('sofia_deals.html',
                           top_trips=cheapest_trips_list,
                           search_month=search_month_str,
                           duration_from=duration_from,
                           duration_to=duration_to,
                           now=datetime.utcnow())

# === Notification Configuration Route ===
@app.route('/configure_notifications', methods=['GET', 'POST'])
def configure_notifications():
    if request.method == 'POST':
        # --- Add New Rule Logic ---
        origin_iata = request.form.get('origin_iata', '').strip().upper()
        destination_iata = request.form.get('destination_iata', '').strip().upper()
        search_month_str = request.form.get('outbound_month')
        duration_from_str = request.form.get('duration_from')
        duration_to_str = request.form.get('duration_to')
        threshold_str = request.form.get('threshold')

        errors = []
        iata_pattern = re.compile(r"^[A-Za-z]{3}$")

        # --- Validation --- #
        if not origin_iata or not iata_pattern.match(origin_iata):
            errors.append("Valid 3-letter Origin IATA is required.")
        if not destination_iata or not iata_pattern.match(destination_iata):
            errors.append("Valid 3-letter Destination IATA is required.")
        if origin_iata and destination_iata and origin_iata == destination_iata:
             errors.append("Origin and Destination cannot be the same.")

        try:
            year, month = map(int, search_month_str.split('-'))
            if not (1 <= month <= 12) or year < date.today().year:
                raise ValueError("Invalid month/year")
        except (ValueError, TypeError, AttributeError):
            errors.append("Valid Month (YYYY-MM) is required.")

        try:
            duration_from = int(duration_from_str)
            duration_to = int(duration_to_str)
            if duration_from < 1 or duration_to < 1 or duration_from > duration_to:
                 raise ValueError("Invalid duration range")
        except (ValueError, TypeError):
             errors.append("Valid Min/Max Durations (positive numbers, Min <= Max) are required.")

        try:
            threshold = float(threshold_str)
            if threshold <= 0:
                raise ValueError("Threshold must be positive")
        except (ValueError, TypeError):
            errors.append("Valid positive Price Threshold is required.")
        # --- End Validation --- #

        if not errors:
            # Create new rule
            new_rule = {
                'id': str(uuid.uuid4()), # Generate unique ID
                'origin_iata': origin_iata,
                'destination_iata': destination_iata,
                'search_month': search_month_str,
                'duration_from': duration_from,
                'duration_to': duration_to,
                'threshold': threshold
            }

            # Load current rules, add new one, save
            current_rules = load_notification_rules()
            current_rules.append(new_rule)
            if save_notification_rules(current_rules):
                flash("Notification rule added successfully!", "success")
            else:
                flash("Error saving notification rules. Please check server logs.", "error")
            return redirect(url_for('configure_notifications')) # Redirect after successful add
        else:
            # If validation fails, flash errors and reload form
            for error in errors:
                flash(error, "error")
            # Load existing rules to display them alongside the error message
            notification_rules = load_notification_rules()
            # Return submitted values to repopulate form
            submitted_data = request.form.to_dict()
            return render_template('configure_notifications.html',
                                   notification_rules=notification_rules,
                                   mail_recipient=MAIL_RECIPIENT,
                                   submitted_data=submitted_data, # Pass back submitted data
                                   now=datetime.utcnow())

    # --- GET Request Logic --- #
    notification_rules = load_notification_rules()
    # Pass empty dict for submitted_data on GET
    return render_template('configure_notifications.html', 
                           notification_rules=notification_rules, 
                           mail_recipient=MAIL_RECIPIENT, 
                           submitted_data={},
                           now=datetime.utcnow())

# === Route for Deleting a Notification Rule ===
@app.route('/delete_notification_rule', methods=['POST'])
def delete_notification_rule():
    rule_id_to_delete = request.form.get('rule_id')
    if not rule_id_to_delete:
        flash("Invalid request: Missing rule ID for deletion.", "error")
        return redirect(url_for('configure_notifications'))

    current_rules = load_notification_rules()
    # Filter out the rule with the matching ID
    updated_rules = [rule for rule in current_rules if rule.get('id') != rule_id_to_delete]

    if len(updated_rules) < len(current_rules): # Check if a rule was actually removed
        if save_notification_rules(updated_rules):
            flash("Notification rule deleted successfully.", "success")
        else:
            flash("Error saving rules after deletion. Please check server logs.", "error")
    else:
        flash("Rule not found for deletion.", "warning") # Rule ID didn't match any existing rule

    return redirect(url_for('configure_notifications'))

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
# NOTIFICATION_THRESHOLD = 90.0 # Removed - Threshold is now per-rule

def check_notification_rules(): # Renamed function
    """Scheduled task to check deals based on saved notification rules."""
    print(f"\n[{datetime.now()}] Running background check for configured notification rules...")

    rules = load_notification_rules()
    if not rules:
        print("  No notification rules configured. Skipping checks.")
        return

    global background_deal_findings # Need this to update notified_deals
    
    # --- Loop through each configured rule --- #
    for rule in rules:
        # Extract parameters for this rule
        rule_id = rule.get('id')
        origin_iata = rule.get('origin_iata')
        destination_iata = rule.get('destination_iata')
        search_month_str = rule.get('search_month')
        duration_from = rule.get('duration_from') # Assuming these are stored as int/float now
        duration_to = rule.get('duration_to')
        threshold = rule.get('threshold')
        currency = "EUR" # Assuming EUR for now, could be added to rule later

        # --- Basic validation of rule data --- #
        if not all([rule_id, origin_iata, destination_iata, search_month_str, duration_from, duration_to, threshold]):
            print(f"  Skipping invalid or incomplete rule: {rule}")
            continue

        print(f"  Checking Rule ID {rule_id[:6]}...: {origin_iata} -> {destination_iata} ({search_month_str}, {duration_from}-{duration_to} days, < {threshold} {currency})")

        # --- Calculate Dates --- #
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
        except (AttributeError, ValueError, TypeError) as e:
            print(f"    ERROR: Invalid date/duration in rule {rule_id[:6]}. Skipping. Error: {e}")
            continue # Skip this rule
        # --- End Date Calculation --- #

        # --- API Call --- #
        found_deals_for_this_rule = []
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
                        # Check against the threshold FOR THIS RULE
                        if total_price is not None and total_price < threshold:
                            trip_details = {
                                'origin_iata': fare.get('outbound', {}).get('departureAirport', {}).get('iataCode'),
                                'destination_iata': fare.get('outbound', {}).get('arrivalAirport', {}).get('iataCode'),
                                'total_price': total_price,
                                'currency': fare.get('summary', {}).get('price', {}).get('currencyCode'),
                                'outbound_dep_time': fare.get('outbound', {}).get('departureDate'),
                                'inbound_dep_time': fare.get('inbound', {}).get('departureDate')
                            }
                            found_deals_for_this_rule.append(trip_details)
                    except (KeyError, TypeError):
                        continue # Skip parsing error for specific fare
        except Exception as e:
            print(f"    Error checking API for rule {rule_id[:6]}: {e}")
            continue # Skip this rule if API fails
        # --- End API Call --- #

        # --- Email Notification Logic (per rule) --- #
        newly_found_deals_for_email = []
        if found_deals_for_this_rule:
            for deal in found_deals_for_this_rule:
                # Make notified_deals key more specific including rule ID
                deal_id = f"{rule_id}-{deal['destination_iata']}-{deal['total_price']}-{deal['outbound_dep_time']}"
                if deal_id not in background_deal_findings["notified_deals"]:
                    newly_found_deals_for_email.append(deal)
                    # Note: Adding to notified set happens after potential successful send attempt

        if newly_found_deals_for_email:
            print(f"  Found {len(newly_found_deals_for_email)} new deal(s) matching Rule ID {rule_id[:6]} ({origin_iata}->{destination_iata} < {threshold} {currency}) to notify.")
            if not MAIL_RECIPIENT:
                print("    ERROR: MAIL_RECIPIENT environment variable not set. Cannot send email.")
                continue # Skip email sending for this rule if recipient not set

            subject = f"Ryanair Deal Alert! {origin_iata} -> {destination_iata} flight(s) under {threshold} {currency} found!"
            body_lines = [f"Found {len(newly_found_deals_for_email)} new round trip deal(s) matching your rule ({origin_iata} -> {destination_iata} in {search_month_str}, {duration_from}-{duration_to} days, under {threshold} {currency}):", ""]
            for deal in newly_found_deals_for_email:
                body_lines.append(f"- Price: {deal['total_price']}{deal['currency']} (Outbound: {deal['outbound_dep_time'][:10]}, Inbound: {deal['inbound_dep_time'][:10]})")
            body = "\n".join(body_lines)

            # Send email within app context
            try:
                with app.app_context():
                    msg = Message(subject, recipients=[MAIL_RECIPIENT])
                    msg.body = body
                    mail.send(msg)
                print(f"    Successfully sent email notification to {MAIL_RECIPIENT} for rule {rule_id[:6]}")
                # Update notified set only after successful send attempt
                for deal in newly_found_deals_for_email:
                     deal_id = f"{rule_id}-{deal['destination_iata']}-{deal['total_price']}-{deal['outbound_dep_time']}"
                     background_deal_findings["notified_deals"].add(deal_id)
            except Exception as e:
                print(f"    ERROR sending email notification for rule {rule_id[:6]}: {e}")
                # Decide if you want to retry or just skip marking as notified on failure

    # --- End loop through rules --- #
    background_deal_findings["last_checked"] = datetime.now() # Update overall last checked time
    print(f"[{datetime.now()}] Background check finished.")

# === Background Task for Historical Data Collection ===

def collect_price_history():
    """Scheduled task to collect daily cheapest prices and store in Supabase."""
    print(f"[{datetime.now()}] Attempting to start collect_price_history task...") # ADDED FOR DEBUGGING
    if not supabase: # Check if Supabase client is initialized
        print(f"[{datetime.now()}] Skipping price history collection: Supabase client not available.")
        return

    # --- Configuration (Hardcoded for now) ---
    routes_to_track = [
        {"origin": "SOF", "destination": "BCN"}
        # Add more routes here later if needed
    ]
    # Dynamically calculate next month instead of using a hardcoded value
    today = date.today()
    current_month_start = today.replace(day=1)
    # Add enough days to guarantee getting into the next month, then replace day with 1
    next_month_start = (current_month_start + timedelta(days=35)).replace(day=1) 
    month_to_track_str = next_month_start.strftime('%Y-%m')

    currency = "EUR"
    # -----------------------------------------

    print(f"\n[{datetime.now()}] Running price history collection for {month_to_track_str}...")

    try:
        year, month = map(int, month_to_track_str.split('-'))
        month_date = date(year, month, 1)
        # last_day = get_last_day_of_month(year, month) # Not needed for API call
    except (ValueError, TypeError, AttributeError):
        print(f"  ERROR: Invalid month format '{month_to_track_str}' for history collection.")
        return

    # Helper function (similar to analysis one, but formats for DB)
    def get_and_prepare_daily_prices(orig, dest, month_dt):
        records_to_insert = []
        api_url = ONE_WAY_MONTH_API_TEMPLATE.format(
            origin_iata=orig,
            destination_iata=dest,
            month_date=month_dt.strftime("%Y-%m-%d"),
            currency=currency
        )
        print(f"  Calling History API: {api_url}")
        try:
            response = requests.get(api_url, headers=HEADERS, timeout=20)
            response.raise_for_status()
            data = response.json()
            if 'fares' in data:
                for day_fare in data['fares']:
                    day_num = day_fare.get('day')
                    price_info = day_fare.get('price')
                    if day_num and price_info and price_info['value'] is not None:
                        try:
                            departure_dt = date(month_dt.year, month_dt.month, day_num)
                            record = {
                                # collected_at is handled by DB default
                                'origin_iata': orig,
                                'destination_iata': dest,
                                'departure_date': departure_dt.isoformat(), # Store as YYYY-MM-DD string
                                'price': price_info['value'],
                                'currency': price_info.get('currencyCode', currency),
                                'direction': 'outbound' if orig == route["origin"] else 'inbound'
                            }
                            records_to_insert.append(record)
                        except ValueError: # Handle invalid day numbers (e.g., 31 in Feb)
                            print(f"    Warning: Invalid day number {day_num} for {month_dt.strftime('%Y-%m')}. Skipping.")
                            continue
            return records_to_insert, None # Return records, no error
        except requests.exceptions.RequestException as req_err:
            err_msg = f"Network Error fetching history for {orig}->{dest}: {req_err}"
            print(f"    {err_msg}")
            return [], err_msg # Return empty list, error message
        except Exception as e:
            err_msg = f"Error fetching/parsing history for {orig}->{dest}: {e}"
            print(f"    {err_msg}")
            return [], err_msg # Return empty list, error message

    # --- Collect and Insert Data for each route --- #
    total_inserted = 0
    for route in routes_to_track:
        origin = route["origin"]
        destination = route["destination"]
        
        # Get outbound and inbound prices
        out_records, out_err = get_and_prepare_daily_prices(origin, destination, month_date)
        in_records, in_err = get_and_prepare_daily_prices(destination, origin, month_date)
        
        all_records_for_route = out_records + in_records
        
        if not all_records_for_route:
             print(f"  No price data found or error occurred for {origin}<->{destination}. Skipping DB insert.")
             continue

        # Insert into Supabase
        try:
            print(f"  Attempting to insert {len(all_records_for_route)} records for {origin}<->{destination}...")
            data, count = supabase.table('price_history').insert(all_records_for_route).execute()
            # Note: Supabase python client v1 might return count differently or not at all.
            # V2 execute() returns a tuple like (data, count).
            # We primarily care if an exception occurs.
            print(f"  Successfully inserted records for {origin}<->{destination}.")
            total_inserted += len(all_records_for_route) # Rough count, actual count might differ if some rows fail
        except Exception as db_err:
            print(f"  ERROR inserting price history for {origin}<->{destination} into Supabase: {db_err}")

    print(f"[{datetime.now()}] Price history collection finished. Attempted to insert ~{total_inserted} records.")

# --- Initialize Scheduler ---
logging.basicConfig()
logging.getLogger('apscheduler').setLevel(logging.WARNING) # Reduce APScheduler noise

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(check_notification_rules, 'interval', minutes=2)
# Add job for history collection (e.g., every 2 minutes for debugging)
scheduler.add_job(collect_price_history, 'interval', minutes=2, id='price_history_collector') # Changed from hours=1

try:
    scheduler.start()
    print("Background notification rule checker scheduled to run every 2 minutes.")
    print("Background price history collector scheduled to run every 2 minutes.") # Updated message
    # Shut down the scheduler when exiting the app
    atexit.register(lambda: scheduler.shutdown())
except (KeyboardInterrupt, SystemExit):
    scheduler.shutdown()

# === Price Analysis Route ===

@app.route('/price_analysis')
def price_analysis():
    origin_iata = request.args.get('origin_iata', '').strip().upper()
    destination_iata = request.args.get('destination_iata', '').strip().upper()
    month_str = request.args.get('month', '')

    # If no parameters provided, show the form
    if not all([origin_iata, destination_iata, month_str]):
        today = date.today()
        # Default to next month for the form
        default_month = (today.replace(day=1) + timedelta(days=32)).strftime('%Y-%m')
        # Get form values from query args if partially provided
        form_data = {
            'origin_iata': origin_iata,
            'destination_iata': destination_iata,
            'month': month_str or default_month
        }
        return render_template('price_analysis_form.html', form_data=form_data, now=datetime.utcnow())

    # --- Parameters provided, proceed with analysis --- #
    print(f"Price Analysis Req: {origin_iata} <-> {destination_iata} for {month_str}")
    results = [] # List to hold daily price info: {'day': d, 'out_price': p1, 'in_price': p2}
    errors = []
    currency = "EUR"

    # --- Input Validation --- #
    iata_pattern = re.compile(r"^[A-Za-z]{3}$")
    if not iata_pattern.match(origin_iata) or not iata_pattern.match(destination_iata):
        errors.append("Origin and Destination must be valid 3-letter IATA codes.")
    if origin_iata == destination_iata:
        errors.append("Origin and Destination cannot be the same.")
    
    try:
        year, month = map(int, month_str.split('-'))
        if not (1 <= month <= 12) or year < date.today().year:
            raise ValueError("Invalid month/year")
        month_date = date(year, month, 1)
        last_day = get_last_day_of_month(year, month)
    except (ValueError, TypeError, AttributeError):
        errors.append("Invalid month format. Please use YYYY-MM.")
    # --- End Validation --- #

    if not errors:
        # Helper function to get daily prices for one direction
        def get_daily_prices(orig, dest, month_dt):
            daily_prices = {}
            api_url = ONE_WAY_MONTH_API_TEMPLATE.format(
                origin_iata=orig,
                destination_iata=dest,
                month_date=month_dt.strftime("%Y-%m-%d"),
                currency=currency
            )
            print(f"  Calling Analysis API: {api_url}")
            try:
                response = requests.get(api_url, headers=HEADERS, timeout=20)
                response.raise_for_status()
                data = response.json()
                if 'fares' in data:
                    for day_fare in data['fares']:
                        if day_fare.get('price') and day_fare['price']['value'] is not None:
                            daily_prices[day_fare['day']] = day_fare['price']['value']
                return daily_prices, None # Return prices, no error
            except requests.exceptions.HTTPError as http_err:
                err_msg = f"API Error ({http_err.response.status_code}) for {orig}->{dest}"
                print(f"    {err_msg}")
                return {}, err_msg # Return empty dict, error message
            except Exception as e:
                err_msg = f"Error fetching data for {orig}->{dest}: {e}"
                print(f"    {err_msg}")
                return {}, err_msg # Return empty dict, error message

        # Get outbound and inbound prices
        outbound_prices, out_err = get_daily_prices(origin_iata, destination_iata, month_date)
        inbound_prices, in_err = get_daily_prices(destination_iata, origin_iata, month_date)

        if out_err: errors.append(out_err)
        if in_err: errors.append(in_err)

        # Combine results day by day
        # Check if last_day was successfully calculated before using it
        if 'last_day' in locals(): 
            for day_num in range(1, last_day + 1):
                results.append({
                    'day': day_num,
                    'out_price': outbound_prices.get(day_num), # Will be None if day not found
                    'in_price': inbound_prices.get(day_num)  # Will be None if day not found
                })
            
    # Flash any errors accumulated
    for error in errors:
        flash(error, "error")

    return render_template('price_analysis_results.html',
                           origin_iata=origin_iata,
                           destination_iata=destination_iata,
                           month_str=month_str,
                           results=results,
                           currency=currency,
                           now=datetime.utcnow())

# === API Route for Historical Price Data ===

@app.route('/api/price_history')
def api_price_history():
    if not supabase:
        return jsonify({"error": "Supabase client not available"}), 503

    # Get query parameters
    origin_iata = request.args.get('origin_iata', '').strip().upper()
    destination_iata = request.args.get('destination_iata', '').strip().upper()
    departure_date_str = request.args.get('departure_date', '') # Expect YYYY-MM-DD
    direction = request.args.get('direction', 'outbound').lower() # 'outbound' or 'inbound'

    # --- Basic Validation --- #
    errors = []
    iata_pattern = re.compile(r"^[A-Za-z]{3}$")
    if not origin_iata or not iata_pattern.match(origin_iata):
        errors.append("Missing or invalid origin_iata parameter.")
    if not destination_iata or not iata_pattern.match(destination_iata):
        errors.append("Missing or invalid destination_iata parameter.")
    if direction not in ['outbound', 'inbound']:
        errors.append("Invalid direction parameter. Use 'outbound' or 'inbound'.")
    
    try:
        departure_date_obj = datetime.strptime(departure_date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        errors.append("Missing or invalid departure_date parameter (YYYY-MM-DD).")

    if errors:
        return jsonify({"error": "Invalid parameters", "details": errors}), 400
    # --- End Validation --- #

    # Determine actual origin/destination based on direction for the query
    query_origin = origin_iata if direction == 'outbound' else destination_iata
    query_destination = destination_iata if direction == 'outbound' else origin_iata

    print(f"API Req: History for {query_origin}->{query_destination} on {departure_date_str}")

    # --- Query Supabase --- #
    try:
        query = supabase.table('price_history')\
            .select('collected_at, price')\
            .eq('origin_iata', query_origin)\
            .eq('destination_iata', query_destination)\
            .eq('departure_date', departure_date_str)\
            .order('collected_at', desc=False)
            # .limit(1000) # Optional: Limit results if needed
        
        results = query.execute()

        # Supabase Python client v2 returns an APIResponse object
        # Access data via response.data
        data = results.data
        
        if data:
             # Prepare data for Chart.js (labels = timestamps, data = prices)
             labels = [item['collected_at'] for item in data]
             prices = [item['price'] for item in data]
             return jsonify({"labels": labels, "prices": prices})
        else:
             return jsonify({"labels": [], "prices": [], "message": "No historical data found for these criteria."}), 200

    except Exception as e:
        print(f"Error querying Supabase for price history: {e}")
        return jsonify({"error": "Database query failed"}), 500

# === Route for Price Trend Visualization ===

@app.route('/price_trends')
def price_trends():
    # Just render the form template. Data loading and chart rendering happens via JavaScript.
    today = date.today()
    default_date = (today + timedelta(days=30)).strftime('%Y-%m-%d') # Default to 30 days from now
    # Get form values from query args if user navigates back/refreshes?
    # Might be simpler to just use defaults or let JS handle it.
    form_data = {
        'origin_iata': request.args.get('origin_iata', 'SOF'),
        'destination_iata': request.args.get('destination_iata', 'BCN'),
        'departure_date': request.args.get('departure_date', default_date),
        'direction': request.args.get('direction', 'outbound')
    }
    return render_template('price_trends.html', form_data=form_data, now=datetime.utcnow())

# === Main Execution ===
if __name__ == '__main__':
    # Note: Flask's default reloader might interfere with APScheduler.
    # Consider running with app.run(debug=True, use_reloader=False) if issues arise during development.
    app.run(debug=True, use_reloader=False) 