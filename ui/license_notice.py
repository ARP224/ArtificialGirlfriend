"""AGPL-3.0 section 13 source-code offer.

Server mode mounts three Gradio UIs on the network -- "/" (desktop), "/mobile"
and "/admin" (ui/app.py) -- so every one of them carries this notice. Section 13
requires each user who interacts with the program remotely to be offered the
Corresponding Source of the running version, and "/" is the catch-all mount, so
covering only the mobile UI would miss the path a remote browser hits first.

Rendered as a plain <div> with inline styling on purpose: the mobile stylesheet
hides <footer>, .built-with and a[href*="api"] (ui/mobile_app.py). An element
caught by one of those rules would vanish silently -- which is exactly a
section 13 failure -- so this snippet depends on no page CSS and matches none of
those selectors.

If you fork and modify Artificial Girlfriend, point SOURCE_URL at your own
source; the offer must lead to the source of the version actually running.
"""

from backend.shared.i18n import t

SOURCE_URL = "https://github.com/ARP224/ArtificialGirlfriend"


def license_notice_html() -> str:
    """Return the source-code offer as a self-contained HTML snippet."""
    return (
        '<div style="padding:8px 12px;text-align:center;font-size:11px;'
        'line-height:1.5;opacity:0.55;">'
        f'<a href="{SOURCE_URL}" target="_blank" rel="noopener noreferrer" '
        'style="color:inherit;text-decoration:underline;">'
        f'{t("common.source_code")}</a></div>'
    )
