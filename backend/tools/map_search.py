"""
backend/map_search.py

Google Maps API calls for Map Search Function Calling.
Provides search_places, get_place_details, and get_directions.
Ollama: available on tools-capable models (2026-08-11).
"""

import logging
import requests
import time
from typing import Dict, Any

from backend.shared.constants import MAP_SEARCH_RESULT_MAX_LENGTH

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places/"
DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"

API_TIMEOUT = 10  # seconds

TEXT_SEARCH_FIELD_MASK = (
    "places.id,"
    "places.displayName,"
    "places.formattedAddress,"
    "places.primaryTypeDisplayName,"
    "places.priceLevel,"
    "places.rating,"
    "places.userRatingCount,"
    "places.currentOpeningHours"
)

PLACE_DETAILS_FIELD_MASK = "reviews,generativeSummary"

# API enum → カタログ短文キー (文言は locales/prompts/*.json fmt.map.*)
PRICE_LEVEL_KEYS = {
    "PRICE_LEVEL_FREE": "fmt.map.price_free",
    "PRICE_LEVEL_INEXPENSIVE": "fmt.map.price_inexpensive",
    "PRICE_LEVEL_MODERATE": "fmt.map.price_moderate",
    "PRICE_LEVEL_EXPENSIVE": "fmt.map.price_expensive",
    "PRICE_LEVEL_VERY_EXPENSIVE": "fmt.map.price_very_expensive",
}

MODE_DISPLAY_KEYS = {
    "walking": "fmt.map.mode_walking",
    "driving": "fmt.map.mode_driving",
    "transit": "fmt.map.mode_transit",
}


# ---------------------------------------------------------------------------
# Text truncation helper
# ---------------------------------------------------------------------------

def _truncate_text(text: str, max_chars: int, language: str) -> str:
    """Truncate text at a boundary, appending marker if truncated."""
    if len(text) <= max_chars:
        return text

    truncated = text[:max_chars]

    for sep in ("\n\n", "\n", "。", "."):
        last_pos = truncated.rfind(sep)
        if last_pos > max_chars * 0.5:
            truncated = truncated[: last_pos + len(sep)]
            break

    from backend.shared.prompt_i18n import prompt_text
    return truncated.rstrip() + "\n" + prompt_text("fmt.map.truncated", language)


# ---------------------------------------------------------------------------
# search_places  (Places API New - Text Search)
# ---------------------------------------------------------------------------

def search_places(query: str, lat: float, lng: float, api_key: str, language: str) -> Dict[str, Any]:
    """Search for places near the user's current location.

    Uses Google Places API (New) Text Search with locationBias.

    Args:
        query: Search query (e.g. "ラーメン", "おしゃれなカフェ").
        lat: Latitude of user's current location.
        lng: Longitude of user's current location.
        api_key: Google Maps API key.
        language: Prompt language (result labels + Google languageCode).

    Returns:
        {"success": True, "result": "formatted text"} or
        {"success": False, "error": "error message"}
    """
    from backend.shared.prompt_i18n import prompt_text

    if not query or not query.strip():
        return {"success": False, "error": prompt_text("fmt.map.empty_query", language)}

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": TEXT_SEARCH_FIELD_MASK,
    }

    body = {
        "textQuery": query.strip(),
        "locationBias": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": 1500.0,
            }
        },
        "pageSize": 10,
        "languageCode": language,
    }

    try:
        response = requests.post(TEXT_SEARCH_URL, json=body, headers=headers, timeout=API_TIMEOUT)

        if response.status_code in (401, 403):
            logger.warning(f"[MapSearch] Auth error {response.status_code} for search_places")
            return {"success": False, "error": prompt_text("fmt.map.bad_api_key", language)}

        response.raise_for_status()
        data = response.json()

    except requests.exceptions.Timeout:
        logger.warning("[MapSearch] search_places timeout")
        return {"success": False, "error": prompt_text("fmt.map.timeout", language)}
    except requests.exceptions.ConnectionError:
        logger.warning("[MapSearch] search_places connection error")
        return {"success": False, "error": prompt_text("fmt.map.unreachable", language)}
    except requests.exceptions.HTTPError as e:
        logger.error(f"[MapSearch] search_places HTTP error: {e}")
        return {"success": False, "error": prompt_text("fmt.map.http_error", language, status=e.response.status_code)}
    except Exception as e:
        logger.error(f"[MapSearch] search_places error: {e}")
        return {"success": False, "error": prompt_text("fmt.map.search_error", language)}

    places = data.get("places", [])
    if not places:
        return {"success": False, "error": prompt_text("fmt.map.no_results", language)}

    # Format results
    lines = [prompt_text("fmt.map.results_header", language, query=query, count=len(places))]
    for i, place in enumerate(places, 1):
        place_id = place.get("id", "")
        name = _extract_display_text(place.get("displayName"))
        address = place.get("formattedAddress", "")
        place_type = _extract_display_text(place.get("primaryTypeDisplayName"))
        price_key = PRICE_LEVEL_KEYS.get(place.get("priceLevel", ""))
        price_level = prompt_text(price_key, language) if price_key else ""
        rating = place.get("rating")
        rating_count = place.get("userRatingCount")
        hours_today = _extract_today_hours(place.get("currentOpeningHours"))

        lines.append(f"\n{i}. {name} [ID:{place_id}]")
        if address:
            lines.append(prompt_text("fmt.map.address", language, address=address))

        detail_parts = []
        if place_type:
            detail_parts.append(prompt_text("fmt.map.type", language, type=place_type))
        if rating is not None:
            rating_str = f"★{rating}"
            if rating_count is not None:
                rating_str += prompt_text("fmt.map.rating_count", language, count=rating_count)
            detail_parts.append(prompt_text("fmt.map.rating", language, rating=rating_str))
        if price_level:
            detail_parts.append(prompt_text("fmt.map.price", language, price=price_level))
        if detail_parts:
            lines.append(f"   {' | '.join(detail_parts)}")

        if hours_today:
            lines.append(prompt_text("fmt.map.hours", language, hours=hours_today))

    result_text = "\n".join(lines)
    result_text = _truncate_text(result_text, MAP_SEARCH_RESULT_MAX_LENGTH, language)

    return {"success": True, "result": result_text}


# ---------------------------------------------------------------------------
# get_place_details  (Places API New - Place Details)
# ---------------------------------------------------------------------------

def get_place_details(place_id: str, api_key: str, language: str) -> Dict[str, Any]:
    """Get reviews and AI summary for a specific place.

    Args:
        place_id: Google Place ID from search_places results.
        api_key: Google Maps API key.
        language: Prompt language (result labels + Google languageCode).

    Returns:
        {"success": True, "result": "formatted text"} or
        {"success": False, "error": "error message"}
    """
    from backend.shared.prompt_i18n import prompt_text

    if not place_id or not place_id.strip():
        return {"success": False, "error": prompt_text("fmt.map.no_place_id", language)}

    url = f"{PLACE_DETAILS_URL}{place_id.strip()}"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": PLACE_DETAILS_FIELD_MASK,
    }

    params = {"languageCode": language}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=API_TIMEOUT)

        if response.status_code in (401, 403):
            logger.warning(f"[MapSearch] Auth error {response.status_code} for get_place_details")
            return {"success": False, "error": prompt_text("fmt.map.bad_api_key", language)}

        if response.status_code == 404:
            return {"success": False, "error": prompt_text("fmt.map.place_not_found", language)}

        response.raise_for_status()
        data = response.json()

    except requests.exceptions.Timeout:
        logger.warning("[MapSearch] get_place_details timeout")
        return {"success": False, "error": prompt_text("fmt.map.timeout", language)}
    except requests.exceptions.ConnectionError:
        logger.warning("[MapSearch] get_place_details connection error")
        return {"success": False, "error": prompt_text("fmt.map.unreachable", language)}
    except requests.exceptions.HTTPError as e:
        logger.error(f"[MapSearch] get_place_details HTTP error: {e}")
        return {"success": False, "error": prompt_text("fmt.map.http_error", language, status=e.response.status_code)}
    except Exception as e:
        logger.error(f"[MapSearch] get_place_details error: {e}")
        return {"success": False, "error": prompt_text("fmt.map.details_error", language)}

    # Format result
    lines = []

    # AI Summary
    gen_summary = data.get("generativeSummary")
    if gen_summary:
        overview = gen_summary.get("overview", {})
        summary_text = overview.get("text", "") if isinstance(overview, dict) else ""
        if summary_text:
            lines.append(prompt_text("fmt.map.ai_summary", language, summary=summary_text))
        else:
            lines.append(prompt_text("fmt.map.ai_summary_unavailable", language))
    else:
        lines.append(prompt_text("fmt.map.ai_summary_unavailable", language))

    # Reviews
    reviews = data.get("reviews", [])
    if reviews:
        lines.append("\n" + prompt_text("fmt.map.reviews_header", language))
        for i, review in enumerate(reviews, 1):
            rating = review.get("rating", 0)
            author = review.get("authorAttribution", {}).get("displayName", prompt_text("fmt.map.anonymous", language))
            # review text
            review_text_obj = review.get("originalText") or review.get("text", {})
            review_text = review_text_obj.get("text", "") if isinstance(review_text_obj, dict) else ""
            # date
            publish_time = review.get("publishTime", "")
            date_str = publish_time[:10] if publish_time else ""

            if not review_text:
                review_text = prompt_text("fmt.map.no_review_text", language)
            lines.append(prompt_text("fmt.map.review_line", language,
                                     i=i, rating=rating, author=author, date=date_str, text=review_text))
    else:
        lines.append("\n" + prompt_text("fmt.map.no_reviews", language))

    result_text = "\n".join(lines)
    result_text = _truncate_text(result_text, MAP_SEARCH_RESULT_MAX_LENGTH, language)

    return {"success": True, "result": result_text}


# ---------------------------------------------------------------------------
# get_directions  (Directions API Legacy)
# ---------------------------------------------------------------------------

def get_directions(place_id: str, lat: float, lng: float,
                   mode: str, api_key: str, language: str) -> Dict[str, Any]:
    """Get route directions from current location to a destination.

    Args:
        place_id: Destination Place ID.
        lat: Origin latitude.
        lng: Origin longitude.
        mode: Travel mode ("walking", "driving", "transit").
        api_key: Google Maps API key.
        language: Prompt language (result labels + Google language param).

    Returns:
        {"success": True, "result": "formatted text"} or
        {"success": False, "error": "error message"}
    """
    from backend.shared.prompt_i18n import prompt_text

    if not place_id or not place_id.strip():
        return {"success": False, "error": prompt_text("fmt.map.no_place_id", language)}

    if mode not in ("walking", "driving", "transit"):
        mode = "walking"

    params = {
        "origin": f"{lat},{lng}",
        "destination": f"place_id:{place_id.strip()}",
        "mode": mode,
        "language": language,
        "key": api_key,
    }

    try:
        response = requests.get(DIRECTIONS_URL, params=params, timeout=API_TIMEOUT)

        if response.status_code in (401, 403):
            logger.warning(f"[MapSearch] Auth error {response.status_code} for get_directions")
            return {"success": False, "error": prompt_text("fmt.map.bad_api_key", language)}

        response.raise_for_status()
        data = response.json()

    except requests.exceptions.Timeout:
        logger.warning("[MapSearch] get_directions timeout")
        return {"success": False, "error": prompt_text("fmt.map.timeout", language)}
    except requests.exceptions.ConnectionError:
        logger.warning("[MapSearch] get_directions connection error")
        return {"success": False, "error": prompt_text("fmt.map.unreachable", language)}
    except requests.exceptions.HTTPError as e:
        logger.error(f"[MapSearch] get_directions HTTP error: {e}")
        return {"success": False, "error": prompt_text("fmt.map.http_error", language, status=e.response.status_code)}
    except Exception as e:
        logger.error(f"[MapSearch] get_directions error: {e}")
        return {"success": False, "error": prompt_text("fmt.map.route_error", language)}

    status = data.get("status", "")
    if status == "NOT_FOUND":
        return {"success": False, "error": prompt_text("fmt.map.place_not_found", language)}
    if status == "ZERO_RESULTS":
        return {"success": False, "error": prompt_text("fmt.map.no_route", language)}
    if status != "OK":
        error_msg = data.get("error_message", status)
        return {"success": False, "error": prompt_text("fmt.map.directions_status_error", language, error=error_msg)}

    routes = data.get("routes", [])
    if not routes:
        return {"success": False, "error": prompt_text("fmt.map.no_route", language)}

    unknown = prompt_text("fmt.map.unknown", language)
    leg = routes[0].get("legs", [{}])[0]
    distance = leg.get("distance", {}).get("text", unknown)
    duration = leg.get("duration", {}).get("text", unknown)
    route_summary = routes[0].get("summary", "")

    mode_key = MODE_DISPLAY_KEYS.get(mode)
    mode_display = prompt_text(mode_key, language) if mode_key else mode

    lines = [
        prompt_text("fmt.map.directions_header", language),
        prompt_text("fmt.map.mode", language, mode=mode_display),
        prompt_text("fmt.map.distance", language, distance=distance),
        prompt_text("fmt.map.duration", language, duration=duration),
    ]
    if route_summary:
        lines.append(prompt_text("fmt.map.via", language, summary=route_summary))

    return {"success": True, "result": "\n".join(lines)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_display_text(field) -> str:
    """Extract text from Places API display fields (e.g. displayName).

    These fields return {"text": "...", "languageCode": "ja"} objects.
    """
    if isinstance(field, dict):
        return field.get("text", "")
    if isinstance(field, str):
        return field
    return ""


def _extract_today_hours(opening_hours) -> str:
    """Extract today's opening hours string from currentOpeningHours.

    currentOpeningHours.weekdayDescriptions is a list of 7 strings,
    one for each day of the week.
    """
    if not opening_hours or not isinstance(opening_hours, dict):
        return ""

    descriptions = opening_hours.get("weekdayDescriptions", [])
    if not descriptions:
        return ""

    # weekdayDescriptions[0] = Monday, etc.
    # Map Python weekday (0=Mon) to list index
    today_index = time.localtime().tm_wday  # 0=Monday
    if today_index < len(descriptions):
        desc = descriptions[today_index]
        # Format is like "月曜日: 11:00～23:00", strip the day prefix
        if ": " in desc:
            return desc.split(": ", 1)[1]
        if "：" in desc:
            return desc.split("：", 1)[1]
        return desc

    return ""
