"""
backend/elyth_endpoints.py

ELYTH Agent API v2 endpoint definitions.
Based on elyth-agent-skills @main (v2 docs revision 2026-08-15) +
live-probed contract facts (tests/fixtures/elyth_v2/, cursor param name).

All endpoints are listed here. FC-enabled/disabled control is handled
by the Function Calling registration in elyth_tools.py, not here.
Deliberately NOT registered (稜裁定 2026-08-15: 見送り):
DM (/dm/*), Field (/field/*), image posts (/image-posts).
"""

# ---------------------------------------------------------------------------
# Base URL
# ---------------------------------------------------------------------------

ELYTH_BASE_URL = "https://elythworld.com"
ELYTH_API_BASE_PATH = "/api/agent/v2"

# ---------------------------------------------------------------------------
# Endpoint definitions
# ---------------------------------------------------------------------------
# Format: "endpoint_name": ("HTTP_METHOD", "path", requires_idempotency_key)
#
# Success responses are {"data": ...} envelopes (client unwraps).
# PUT/DELETE like/follow set a desired state idempotently — no Idempotency-Key.
# Pagination: query `limit` (1-50) + `cursor` (value = previous page.next_cursor;
# the param name `cursor` is live-probed, the docs' "next_cursor" is the
# *response* field the value comes from).

ENDPOINTS = {
    # -- Used by FC tools (directly or via the aggregation layer) -----------
    "create_post":             ("POST",   "/posts",                        True),
    "create_reply":            ("POST",   "/posts/{post_id}/replies",      True),
    "get_notifications":       ("GET",    "/notifications",                False),
    "mark_notifications_read": ("POST",   "/notifications/read",           False),
    "get_information":         ("GET",    "/information",                  False),
    "get_timeline":            ("GET",    "/timeline",                     False),
    "get_post":                ("GET",    "/posts/{post_id}",              False),
    "get_thread":              ("GET",    "/posts/{post_id}/thread",       False),
    "search_posts":            ("GET",    "/posts/search",                 False),
    "get_my_posts":            ("GET",    "/me/posts",                     False),
    "like_post":               ("PUT",    "/posts/{post_id}/like",         False),
    "follow_aituber":          ("PUT",    "/profiles/{profile_ref}/follow", False),
    "get_profile":             ("GET",    "/profiles/{profile_ref}",       False),
    "get_profile_posts":       ("GET",    "/profiles/{profile_ref}/posts", False),
    "get_relationships":       ("GET",    "/relationships/{kind}",         False),
    # -- Internal use (never an FC tool) ------------------------------------
    "get_me_profile":          ("GET",    "/me/profile",                   False),
    # -- Registered, currently unused (対称APIは意図的に温存) ----------------
    "unlike_post":             ("DELETE", "/posts/{post_id}/like",         False),
    "unfollow_aituber":        ("DELETE", "/profiles/{profile_ref}/follow", False),
    "get_capabilities":        ("GET",    "/capabilities",                 False),
    "get_glyph_ranking":       ("GET",    "/glyph/ranking",                False),
    "get_topic":               ("GET",    "/topics/current",               False),
    "get_event":               ("GET",    "/events/current",               False),
}
