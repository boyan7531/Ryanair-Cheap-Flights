import sys
import requests
import json
from datetime import date, timedelta
import calendar
from flask import Flask, render_template, request, flash, jsonify
import airportsdata # Import the library

app = Flask(__name__)
app.secret_key = 'super secret key' # Needed for flashing messages

# --- Load Airport Data & Prepare City List ---
airports = airportsdata.load()
valid_cities = set() # Use a set for efficient lookup and uniqueness
for icao, data in airports.items():
    if data.get('iata') and data.get('city'): # Only include cities with an IATA code
        valid_cities.add(data['city'])
# Convert set to sorted list for predictable ordering (optional but good)
sorted_cities = sorted(list(valid_cities))
print(f"Loaded {len(airports)} airports and found {len(sorted_cities)} unique cities with IATA codes.")
# ---------------------------------------------

# Ryanair API endpoint for round trip fares
API_ENDPOINT_TEMPLATE = "https://www.ryanair.com/api/farfnd/v4/roundTripFares?departureAirportIataCode={origin_iata}&market=en-gb&adultPaxCount=1&arrivalAirportIataCode={destination_iata}&searchMode=ALL&outboundDepartureDateFrom={out_date_from}&outboundDepartureDateTo={out_date_to}&inboundDepartureDateFrom={in_date_from}&inboundDepartureDateTo={in_date_to}&durationFrom={duration_from}&durationTo={duration_to}&currency={currency}"

# Mimic a browser User-Agent
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36'
}

def get_last_day_of_month(year, month):
    """Helper function to get the last day of a given month."""
    return calendar.monthrange(year, month)[1]

def get_iata_from_city(city_name):
    """Find the IATA code for a given city name using airportsdata.

    Args:
        city_name (str): The name of the city to search for.

    Returns:
        str or None: The IATA code if found, otherwise None.
    """
    city_name_lower = city_name.lower()
    # Optimize lookup slightly by checking the pre-filtered city list first
    if city_name not in valid_cities:
         # Check variations if simple match fails (e.g., case difference already handled by lower())
         # More robust fuzzy matching could be added here if needed
         pass # For now, just rely on the iteration below

    for icao, airport_info in airports.items():
        if airport_info.get('city', '').lower() == city_name_lower and airport_info.get('iata'):
            return airport_info['iata']
    return None # No match found

@app.route('/')
def index():
    """Render the main search form page."""
    today = date.today()
    default_month = (today.replace(day=1) + timedelta(days=32)).strftime('%Y-%m')
    return render_template('index.html', default_month=default_month)

# --- NEW API Endpoint for Cities ---
@app.route('/api/cities')
def get_cities():
    """Return the list of valid city names as JSON."""
    return jsonify(sorted_cities)
# -----------------------------------

@app.route('/search', methods=['POST'])
def search_flights():
    """Handle the form submission, call API, and display results."""
    origin_city = request.form.get('origin_city', '').strip()
    destination_city = request.form.get('destination_city', '').strip()
    outbound_month_str = request.form.get('outbound_month') # YYYY-MM
    duration_from = request.form.get('duration_from', '2')
    duration_to = request.form.get('duration_to', '7')
    currency = "EUR" # Hardcoded for simplicity

    # --- Input Validation (City Name) ---
    if not (origin_city and destination_city and outbound_month_str):
        flash("Please fill in Origin City, Destination City, and Outbound Month.")
        # Pass back entered values to the template
        return render_template('index.html', default_month=outbound_month_str or date.today().strftime('%Y-%m'),
                               origin_city=origin_city, destination_city=destination_city)

    # --- IATA Lookup ---
    origin_iata = get_iata_from_city(origin_city)
    destination_iata = get_iata_from_city(destination_city)

    if not origin_iata:
        flash(f"Could not find an airport IATA code for origin city: '{origin_city}'")
    if not destination_iata:
         flash(f"Could not find an airport IATA code for destination city: '{destination_city}'")

    # If either lookup failed, return to form
    if not origin_iata or not destination_iata:
        return render_template('index.html', default_month=outbound_month_str,
                               origin_city=origin_city, destination_city=destination_city)

    print(f"Found IATA codes: {origin_city} -> {origin_iata}, {destination_city} -> {destination_iata}")

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
                               origin_city=origin_city, destination_city=destination_city)

    # --- Construct API URL (using found IATA codes) ---
    api_url = API_ENDPOINT_TEMPLATE.format(
        origin_iata=origin_iata,             # Use origin_iata
        destination_iata=destination_iata,   # Use destination_iata
        out_date_from=out_date_from.strftime("%Y-%m-%d"),
        out_date_to=out_date_to.strftime("%Y-%m-%d"),
        in_date_from=in_date_from.strftime("%Y-%m-%d"),
        in_date_to=in_date_to.strftime("%Y-%m-%d"),
        duration_from=duration_from,
        duration_to=duration_to,
        currency=currency
    )

    print(f"Calling API: {api_url}") # Log for debugging

    # --- Call API and Process Results ---
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
            # Use the original city names in the error message
            error_message = f"No round trips found matching your criteria for {origin_city} ({origin_iata}) -> {destination_city} ({destination_iata}) in {outbound_month_str}."

    except requests.exceptions.HTTPError as http_err:
        print(f"HTTP error: {http_err} - Status: {http_err.response.status_code}")
        # Use the IATA codes in the error message as they were used in the failed API call
        error_message = f"API Error ({http_err.response.status_code}) for {origin_iata} -> {destination_iata}. Check cities or try later."
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

    # Pass original city names back to template for potential display
    return render_template('results.html', top_trips=top_10_trips, query=request.form,
                           origin_city=origin_city, destination_city=destination_city)

if __name__ == '__main__':
    app.run(debug=True) # debug=True for development, remove for production 