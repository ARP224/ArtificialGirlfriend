"""
ui/status_probe.py

Async connectivity-probe cache for the Conversation-page status indicator.

The indicator must render instantly (it is built inside Gradio handlers), so
API probes never run inline: results live in a provider-keyed cache and are
refreshed by daemon threads. When a probe completes it publishes the
``update_status`` WS event — the browser's hidden trigger re-renders the
indicator with the fresh cache (Auto Prompt方式の自己購読経路).

The cache is PROVIDER-keyed ("openai", "anthropic", ...), not line-keyed:
when the LLM line (ChatGPT character) and the STT line (OpenAI API) both
need OpenAI, one HTTP probe serves both lines — they can never disagree.

Stale-while-revalidate: an expired entry keeps being displayed while a
background refresh runs, so "checking" is only ever visible before the
first result of a provider (and right after invalidate()).
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

# Probe results stay fresh for this long. No periodic polling: probes are
# only started from an indicator render (page load, character switch,
# conversation start, STT engine switch, API key save).
CACHE_TTL_SECONDS = 60

_lock = threading.Lock()
_cache = {}       # provider -> {"state": str, "model_count": int, "timestamp": float}
_in_flight = set()  # providers with a probe thread currently running
_last_render_ts = 0.0  # monotonic time of the last get_probe_state call (= render)


def get_probe_state(provider: str) -> dict:
    """Return the cached probe result for a provider, refreshing as needed.

    Never blocks. Returns ``{"state": "checking", "model_count": 0}`` until
    the first result arrives; afterwards always the last known result (a
    stale entry additionally starts a background refresh).
    """
    global _last_render_ts
    now = time.monotonic()
    with _lock:
        _last_render_ts = now
        entry = _cache.get(provider)
        fresh = entry is not None and (now - entry["timestamp"]) < CACHE_TTL_SECONDS
        if not fresh:
            _start_probe_locked(provider)
        if entry is not None:
            return {"state": entry["state"], "model_count": entry["model_count"]}
    return {"state": "checking", "model_count": 0}


def invalidate(provider: str) -> None:
    """Drop a provider's cached result (e.g. its API key was just saved).

    After a key change the old verdict is meaningless — the next render
    deliberately shows "checking" instead of a stale 🔴/🟢.
    """
    with _lock:
        _cache.pop(provider, None)


def _start_probe_locked(provider: str) -> None:
    """Start a background probe unless one is already running (caller holds _lock)."""
    if provider in _in_flight:
        return
    _in_flight.add(provider)
    threading.Thread(target=_probe_worker, args=(provider,), daemon=True,
                     name=f"status-probe-{provider}").start()


def _probe_worker(provider: str) -> None:
    # log-and-continue: a failing probe (even during app shutdown) must never
    # take anything else down with it.
    try:
        from backend.shared.api_settings import probe_provider_api, probe_elevenlabs
        result = probe_elevenlabs() if provider == "elevenlabs" else probe_provider_api(provider)
        write_ts = time.monotonic()
        with _lock:
            _cache[provider] = {
                "state": result.get("state", "unreachable"),
                "model_count": result.get("model_count", 0),
                "timestamp": write_ts,
            }
        _publish_until_rendered(provider, write_ts)
    except Exception as e:
        logger.debug(f"Status probe worker failed for {provider}: {e}")
    finally:
        with _lock:
            _in_flight.discard(provider)


def _publish_until_rendered(provider: str, write_ts: float) -> None:
    """Publish update_status until a render has picked up the new cache.

    The WS push is best-effort at every layer: no clients / a 1s broadcast
    timeout (one dead session stalls the whole broadcast) / half-open
    sockets all lose it silently — 実機 2026-07-19: the Whisper API line
    stayed on 確認中 offline until a manual character switch. A render
    calling get_probe_state AFTER the cache write is the only real ACK, so
    retry against that; past the last attempt the next natural render (any
    UI action or WS reconnect) shows the cached verdict anyway.
    """
    from backend.shared.ui_events import publish_ui_update
    for delay in (0.0, 3.0, 6.0):
        if delay:
            time.sleep(delay)
        with _lock:
            if _last_render_ts >= write_ts:
                return
        try:
            publish_ui_update("update_status", reason=f"probe:{provider}")
        except Exception as e:
            logger.debug(f"update_status publish failed for {provider}: {e}")
