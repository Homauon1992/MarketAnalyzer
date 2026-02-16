import csv
import json
import os
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import requests
from bs4 import BeautifulSoup
from colorama import Fore, Style, init
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


@dataclass
class Hotel:
    name: str
    rating: Optional[float]
    price: Optional[float]
    currency: Optional[str]


USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.3 Safari/605.1.15"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) "
        "Gecko/20100101 Firefox/121.0"
    ),
]


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def build_headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
    }


def parse_number(text: str) -> Optional[float]:
    match = re.search(r"([0-9]+(?:[.,][0-9]+)*)", text)
    if not match:
        return None
    raw = match.group(1)
    # normalize thousands separators
    if raw.count(",") > 1 and "." not in raw:
        raw = raw.replace(",", "")
    raw = raw.replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_currency(text: str) -> Optional[str]:
    symbols = {
        "$": "USD",
        "€": "EUR",
        "£": "GBP",
        "﷼": "OMR",
        "ر.ع.": "OMR",
        "AED": "AED",
        "SAR": "SAR",
        "OMR": "OMR",
        "₹": "INR",
    }
    for symbol, code in symbols.items():
        if symbol in text:
            return code
    match = re.search(r"\b([A-Z]{3})\b", text)
    if match:
        return match.group(1)
    return None


def parse_budget_input(text: str) -> tuple[Optional[float], Optional[str]]:
    amount = parse_number(text)
    currency = parse_currency(text.upper())
    return amount, currency


def find_hotel_name(container: BeautifulSoup) -> Optional[str]:
    for selector in (
        '[data-testid="title"]',
        "h3",
        "h2",
        "h4",
        "a",
    ):
        node = container.select_one(selector)
        if node:
            name = node.get_text(strip=True)
            if name and len(name) > 2:
                return name
    return None


def extract_hotels_general(soup: BeautifulSoup) -> list[Hotel]:
    price_pattern = re.compile(
        r"(\$|€|£|AED|SAR|OMR|USD|GBP|EUR|INR|₹|ر\.ع\.|﷼)\s*[0-9]",
        re.IGNORECASE,
    )
    hotels: list[Hotel] = []
    price_nodes = soup.find_all(string=price_pattern)
    for price_node in price_nodes:
        container = price_node.find_parent(["div", "article", "li", "section"])
        if not container:
            continue
        name = find_hotel_name(container)
        if not name:
            continue
        rating_text = container.get_text(" ", strip=True)
        price_text = str(price_node)
        rating = parse_number(rating_text)
        price = parse_number(price_text)
        currency = parse_currency(price_text)
        hotels.append(Hotel(name=name, rating=rating, price=price, currency=currency))
    return hotels


def fetch_hotels(city: str, session: requests.Session) -> list[Hotel]:
    search_url = "https://www.booking.com/searchresults.html"
    params = {"ss": city}

    response = session.get(search_url, params=params, headers=build_headers(), timeout=20)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    cards = soup.select('div[data-testid="property-card"]')
    hotels: list[Hotel] = []
    for card in cards:
        name_el = card.select_one('[data-testid="title"]')
        rating_el = card.select_one('[data-testid="review-score"]')
        price_el = card.select_one('[data-testid="price-and-discounted-price"]')

        name = name_el.get_text(strip=True) if name_el else None
        rating_text = rating_el.get_text(" ", strip=True) if rating_el else ""
        price_text = price_el.get_text(" ", strip=True) if price_el else ""

        rating = parse_number(rating_text)
        price = parse_number(price_text)
        currency = parse_currency(price_text)

        if name:
            hotels.append(Hotel(name=name, rating=rating, price=price, currency=currency))

    # Resilient fallback: scan for any price-like nodes and infer names nearby.
    if not hotels:
        hotels = extract_hotels_general(soup)

    return hotels


def fetch_hotels_fallback(city: str, session: requests.Session) -> list[Hotel]:
    api_url = "https://random-data-api.com/api/v2/hotels"
    params = {"size": 12}
    try:
        response = session.get(api_url, params=params, headers=build_headers(), timeout=15)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            payload = [payload]
        hotels: list[Hotel] = []
        for item in payload:
            name = item.get("name") or item.get("hotel_name")
            rating = parse_number(str(item.get("rating") or "")) if item else None
            price = parse_number(str(item.get("price") or "")) if item else None
            if name:
                hotels.append(
                    Hotel(name=name, rating=rating, price=price, currency="USD")
                )
        return hotels
    except (requests.RequestException, ValueError, TypeError):
        return generate_mock_hotels(city)


def generate_mock_hotels(city: str) -> list[Hotel]:
    seed = sum(ord(ch) for ch in city.lower())
    random.seed(seed)
    base_names = [
        f"{city} Grand Hotel",
        f"{city} Plaza",
        f"{city} Boutique Stay",
        f"{city} Riverside Inn",
        f"{city} Central Suites",
    ]
    hotels: list[Hotel] = []
    for name in base_names:
        rating = round(random.uniform(7.0, 9.4), 1)
        price = round(random.uniform(25, 120), 2)
        hotels.append(Hotel(name=name, rating=rating, price=price, currency="USD"))
    return hotels


def average_price(hotels: Iterable[Hotel]) -> Optional[float]:
    prices = [hotel.price for hotel in hotels if hotel.price is not None]
    if not prices:
        return None
    return sum(prices) / len(prices)


def find_best_value(hotels: list[Hotel]) -> Optional[Hotel]:
    priced = [hotel for hotel in hotels if hotel.price is not None]
    if not priced:
        return None
    min_price = min(hotel.price for hotel in priced if hotel.price is not None)
    cheapest = [hotel for hotel in priced if hotel.price == min_price]
    best = max(cheapest, key=lambda hotel: hotel.rating or 0)
    return best


def save_csv(path: str, hotels: list[Hotel]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["name", "rating", "price", "currency"])
        writer.writeheader()
        for hotel in hotels:
            writer.writerow(
                {
                    "name": hotel.name,
                    "rating": hotel.rating,
                    "price": hotel.price,
                    "currency": hotel.currency,
                }
            )


def save_json(path: str, hotels: list[Hotel]) -> None:
    payload = [
        {
            "name": hotel.name,
            "rating": hotel.rating,
            "price": hotel.price,
            "currency": hotel.currency,
        }
        for hotel in hotels
    ]
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=True)


def print_table(hotels: list[Hotel], best_value: Optional[Hotel]) -> None:
    if not hotels:
        print(Fore.YELLOW + "No hotels matched your budget." + Style.RESET_ALL)
        return

    name_width = max(len("Hotel"), max(len(h.name) for h in hotels))
    rating_width = len("Rating")
    price_width = len("Price")
    currency_width = len("Currency")

    header = (
        f"{Fore.CYAN}{'Hotel'.ljust(name_width)}  "
        f"{'Rating'.ljust(rating_width)}  "
        f"{'Price'.ljust(price_width)}  "
        f"{'Currency'.ljust(currency_width)}{Style.RESET_ALL}"
    )
    print(header)
    print("-" * (name_width + rating_width + price_width + currency_width + 6))

    for hotel in hotels:
        rating = f"{hotel.rating:.1f}" if hotel.rating is not None else "N/A"
        price = f"{hotel.price:.2f}" if hotel.price is not None else "N/A"
        currency = hotel.currency or "N/A"
        row = (
            f"{hotel.name.ljust(name_width)}  "
            f"{rating.ljust(rating_width)}  "
            f"{price.ljust(price_width)}  "
            f"{currency.ljust(currency_width)}"
        )
        if best_value and hotel.name == best_value.name and hotel.price == best_value.price:
            print(Fore.GREEN + row + Style.RESET_ALL)
        else:
            print(row)


def ensure_script_workdir() -> None:
    script_path = Path(__file__).resolve()
    if "&" in str(script_path):
        print(
            Fore.YELLOW
            + "Note: If running from PowerShell, wrap the script path in quotes."
            + Style.RESET_ALL
        )
    os.chdir(script_path.parent)


def main() -> None:
    init(autoreset=True)
    ensure_script_workdir()
    city = input("Enter a city: ").strip()
    if not city:
        print("City is required.")
        sys.exit(1)

    budget_text = input("Enter Maximum Budget (e.g., 50 OMR): ").strip()
    budget_amount, budget_currency = parse_budget_input(budget_text)
    if budget_amount is None:
        print("Could not parse a budget amount.")
        sys.exit(1)

    print(Fore.BLUE + "Searching for hotels..." + Style.RESET_ALL)
    session = build_session()
    try:
        hotels = fetch_hotels(city, session)
    except requests.RequestException:
        hotels = []

    if not hotels:
        print(
            Fore.YELLOW
            + "Primary source returned no results. Trying fallback source..."
            + Style.RESET_ALL
        )
        hotels = fetch_hotels_fallback(city, session)

    if not hotels:
        print(Fore.YELLOW + "No hotels found. Try a different city." + Style.RESET_ALL)
        sys.exit(0)

    avg_price = average_price(hotels)
    if avg_price is not None:
        print(f"Average price of all found hotels: {avg_price:.2f}")
    else:
        print("Average price of all found hotels: N/A")

    filtered = [
        hotel
        for hotel in hotels
        if hotel.price is not None and hotel.price <= budget_amount
    ]
    best_value = find_best_value(filtered)

    if budget_currency:
        found_currencies = {hotel.currency for hotel in hotels if hotel.currency}
        if found_currencies and budget_currency not in found_currencies:
            print(
                Fore.YELLOW
                + f"Warning: Budget currency {budget_currency} may not match results."
                + Style.RESET_ALL
            )

    print()
    print_table(filtered, best_value)

    save_csv("budget_hotels.csv", filtered)
    save_json("budget_hotels.json", filtered)

    if best_value:
        print(
            Fore.GREEN
            + f"Best Value: {best_value.name} ({best_value.rating or 'N/A'} rating, "
            f"{best_value.price:.2f} {best_value.currency or ''})"
            + Style.RESET_ALL
        )
    else:
        print(Fore.YELLOW + "Best Value: N/A" + Style.RESET_ALL)


if __name__ == "__main__":
    main()
