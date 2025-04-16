import sys
import requests # Import requests
import json     # Import json for parsing
from datetime import date

# --- User Input Parameters (Modify these to test) ---
ORIGIN_IATA = "SOF"  # Example: Sofia
DESTINATION_IATA = "BCN" # Example: Barcelona
SEARCH_YEAR = 2025
SEARCH_MONTH = 5   # May
CURRENCY = "EUR"
# ----------------------------------------------------

# Ryanair API endpoint for cheapest fares per day
API_ENDPOINT_TEMPLATE = "https://www.ryanair.com/api/farfnd/v4/oneWayFares/{origin}/{destination}/cheapestPerDay?outboundMonthOfDate={date}&currency={currency}"

# Mimic a browser User-Agent (important to avoid blocking)
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36'
}

def find_cheapest_flights(origin, destination, year, month, currency):
    """
    Finds the cheapest Ryanair flights per day for a given month and route using direct API call.
    """
    search_date = date(year, month, 1)
    formatted_date = search_date.strftime("%Y-%m-%d")

    # Construct the specific API URL
    api_url = API_ENDPOINT_TEMPLATE.format(
        origin=origin,
        destination=destination,
        date=formatted_date,
        currency=currency
    )

    print(f"Searching for cheapest flights from {origin} to {destination} for {search_date.strftime('%B %Y')}...")
    print(f"Calling API: {api_url}")

    try:
        # Make the GET request to the Ryanair API
        response = requests.get(api_url, headers=HEADERS, timeout=20) # Added timeout
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

        # Parse the JSON response
        data = response.json()

        # --- Process the response data ---
        # The structure is based on the JSON example you provided:
        # {"outbound": {"fares": [{"day": "...", "price": {"value": ...}}, ...]}}
        if 'outbound' not in data or 'fares' not in data['outbound']:
            print("Error: Unexpected API response format. 'outbound' or 'fares' key missing.")
            print(f"Response: {data}")
            return

        daily_fares = data['outbound']['fares']

        if not daily_fares:
            print("No flight data found in the API response for this route/month.")
            return

        min_price = sys.float_info.max
        cheapest_fares = [] # Store tuples of (day_str, price_value)

        print("\nDaily cheapest fares found:")
        for fare_info in daily_fares:
            day_str = fare_info.get('day')
            price_data = fare_info.get('price')
            price_value = price_data.get('value') if price_data else None
            currency_code = price_data.get('currencyCode') if price_data else currency # Fallback

            if day_str and price_value is not None:
                 print(f"- {day_str}: {price_value} {currency_code}")
                 if price_value < min_price:
                     min_price = price_value
                     cheapest_fares = [(day_str, price_value)] # New minimum
                 elif price_value == min_price:
                     cheapest_fares.append((day_str, price_value)) # Same minimum
            elif day_str:
                 # Handle cases where the fare is unavailable or sold out for the day
                 status = "Sold Out" if fare_info.get('soldOut', False) else "Unavailable"
                 print(f"- {day_str}: {status}")
            else:
                print(f"Warning: Skipping fare entry with missing data: {fare_info}")


        # After checking all fares
        if cheapest_fares:
            print(f"\n--------------------------------------------------")
            print(f"Lowest price found: {min_price} {currency_code}") # Use currency from data if available
            print(f"Available on date(s): {', '.join([fare_info[0] for fare_info in cheapest_fares])}")
            print(f"--------------------------------------------------")
        else:
            print("\nNo available flights with prices found for the cheapest calculation.")

    except requests.exceptions.HTTPError as http_err:
        print(f"\nHTTP error occurred: {http_err}")
        print(f"Status Code: {response.status_code}")
        print(f"Response Text: {response.text[:500]}...") # Print beginning of response
        print("This might be due to invalid parameters (airports, date), rate limiting, or API changes.")
    except requests.exceptions.ConnectionError as conn_err:
        print(f"\nConnection error occurred: {conn_err}")
        print("Check your internet connection.")
    except requests.exceptions.Timeout as timeout_err:
        print(f"\nRequest timed out: {timeout_err}")
    except requests.exceptions.RequestException as req_err:
        print(f"\nAn error occurred during the request: {req_err}")
    except json.JSONDecodeError:
        print("\nError: Failed to parse the API response as JSON.")
        print(f"Response Text: {response.text[:500]}...") # Print beginning of response
    except KeyError as key_err:
        print(f"\nError: Unexpected key missing in API response data: {key_err}")
        print(f"Response Data: {data}")
    except Exception as e:
        # Catch other potential errors
        print(f"\nAn unexpected error occurred: {e}")
        print("Check the input parameters and API availability.")


if __name__ == "__main__":
    find_cheapest_flights(ORIGIN_IATA, DESTINATION_IATA, SEARCH_YEAR, SEARCH_MONTH, CURRENCY) 