"""
Location Manager - 位置情報管理機能

iPhoneから受信したGPS座標をGoogle Maps Geocoding APIで
住所（キャラクターのプロンプト言語）に変換し、
LLMプロンプトに注入するための機能を提供します。
"""

import logging
import requests
import time
import threading
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
GEOCODE_TIMEOUT = 5  # seconds

_location_lock = threading.Lock()

# Current location state
_current_location: Optional[Dict[str, Any]] = None
_recorded: bool = True  # True = already recorded to conversation history


def reverse_geocode(lat: float, lng: float, api_key: str, language: str) -> Dict[str, Any]:
    """Reverse geocode coordinates to an address using Google Maps API.

    Args:
        lat: Latitude.
        lng: Longitude.
        api_key: Google Maps Geocoding API key.
        language: Prompt language (Google language param + fallback text).

    Returns:
        Dict with 'success' bool and 'address' or 'error' string.
    """
    from backend.shared.prompt_i18n import prompt_text
    try:
        response = requests.get(GEOCODE_URL, params={
            "latlng": f"{lat},{lng}",
            "key": api_key,
            "language": language,
            "result_type": "street_address|sublocality|locality"
        }, timeout=GEOCODE_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        if data.get("status") == "OK" and data.get("results"):
            address = data["results"][0].get("formatted_address", "")
            return {"success": True, "address": address}
        elif data.get("status") == "ZERO_RESULTS":
            return {"success": True,
                    "address": prompt_text("fmt.location.unknown_address", language, lat=lat, lng=lng)}
        else:
            error_msg = data.get("error_message", data.get("status", "Unknown error"))
            logger.warning(f"Geocoding failed for ({lat}, {lng}): {error_msg}")
            return {"success": False, "error": error_msg}
    except requests.exceptions.Timeout:
        logger.warning(f"Geocoding request timed out for ({lat}, {lng})")
        return {"success": False, "error": "Geocoding request timed out"}
    except requests.exceptions.ConnectionError:
        logger.warning(f"Cannot connect to Google Maps API for ({lat}, {lng})")
        return {"success": False, "error": "Cannot connect to Google Maps API"}
    except Exception as e:
        logger.error(f"Unexpected error geocoding ({lat}, {lng}): {e}")
        return {"success": False, "error": str(e)}


def update_location(lat: float, lng: float, api_key: str, language: str) -> Dict[str, Any]:
    """Update the current location with reverse geocoding.

    Called from WebSocket handler when location data arrives from mobile client.
    Multiple calls before a prompt is built will overwrite previous data.

    Args:
        lat: Latitude from GPS.
        lng: Longitude from GPS.
        api_key: Google Maps API key.
        language: Prompt language for the geocoded address.

    Returns:
        Dict with 'success' bool and 'address' or 'error'.
    """
    global _current_location, _recorded

    geocode_result = reverse_geocode(lat, lng, api_key, language)

    with _location_lock:
        if geocode_result["success"]:
            _current_location = {
                "lat": lat,
                "lng": lng,
                "address": geocode_result["address"],
                "timestamp": time.time()
            }
        else:
            _current_location = {
                "lat": lat,
                "lng": lng,
                "address": None,
                "error": geocode_result["error"],
                "timestamp": time.time()
            }
        _recorded = False  # Mark as unrecorded for next prompt

    return geocode_result


def get_location_for_prompt(language: str) -> str:
    """Get the current location string for injection into the LLM system prompt.

    Returns:
        Formatted location string, or empty string if no location data.
    """
    from backend.shared.prompt_i18n import prompt_text
    with _location_lock:
        if not _current_location:
            return ""

        timestamp = _current_location.get("timestamp", 0)
        time_str = time.strftime("%Y/%m/%d %H:%M", time.localtime(timestamp))

        if _current_location.get("address"):
            return prompt_text("location.line", language,
                               address=_current_location['address'], time=time_str)
        elif _current_location.get("error"):
            return prompt_text("location.failed", language,
                               error=_current_location['error'])
        else:
            return ""


def consume_unrecorded_location() -> Optional[Dict[str, Any]]:
    """Atomically get unrecorded location data and mark as recorded.

    Called from _generate_reply_task() before building the prompt.
    This ensures that multiple button presses between prompts only
    produce one conversation history entry (the latest).

    Returns:
        Location dict if there is unrecorded data, None otherwise.
    """
    global _recorded
    with _location_lock:
        if _current_location and not _recorded:
            _recorded = True
            return dict(_current_location)
        return None


def get_current_location() -> Optional[Dict[str, Any]]:
    """Get the current location data (for status display).

    Returns:
        Copy of current location dict, or None.
    """
    with _location_lock:
        return dict(_current_location) if _current_location else None


def has_valid_location() -> bool:
    """Check if the location slot contains valid coordinates.

    Used by map search tools to determine if location-based
    Function Calling should be enabled.

    Returns:
        True if valid lat/lng are available in the location slot.
    """
    with _location_lock:
        return (_current_location is not None
                and _current_location.get("lat") is not None
                and _current_location.get("lng") is not None)


def has_unrecorded_location() -> bool:
    """Check if there is location data that hasn't been consumed yet.

    Used by the UI to show a location indicator on the next user message.
    """
    with _location_lock:
        return _current_location is not None and not _recorded


def clear_location() -> None:
    """Clear all stored location data."""
    global _current_location, _recorded
    with _location_lock:
        _current_location = None
        _recorded = True
