"""Web App Manifest + icons: make the server-mode pages installable web apps.

Client PCs / phones open AG in a browser. A Web App Manifest lets the
browser "install" the page as its own app window (Chrome/Edge: install
icon in the address bar; macOS Safari: File > Add to Dock; iOS: Add to
Home Screen opens without the Safari chrome). No launcher file has to be
shipped to clients — the browser creates the shortcut/.app itself.

Wiring (server mode only — the outer FastAPI app exists only there):

* Gradio's index.html template hardcodes ``<link rel="manifest"
  href="/manifest.json">`` for every page. ``apply_gradio_index_patch``
  (ui/gradio_patches.py) rewrites that href to the mount-relative
  ``manifest.json``, so ``/`` requests ``/manifest.json`` and ``/mobile/``
  requests ``/mobile/manifest.json`` (``/admin/`` keeps its 404, as today).
* ``register_pwa_routes`` adds those two manifests plus the icon route to
  the outer FastAPI app *before* the Gradio mounts, so the outer routes win
  over Gradio's own (pwa=False → 404) ``manifest.json`` handler — the same
  precedence the ``/apple-touch-icon.png`` route already relies on.

Why not Gradio's built-in ``pwa=True``: its manifest is name + favicon
upscaled to 512px + start_url only (no theme_color / id / scope /
maskable), and the ``/mobile`` mount would fall back to the Gradio logo.

Icons: app_images/pwa_icon_{192,512}.png are LANCZOS downsizes of
app_images/Artificial_Girlfriend_Logo(High Scale).png (1024px), stored
like the other derived sizes (favicon 256 / apple-touch-icon 180). The
logo is a full-bleed square, so the same files serve ``purpose: any`` and
``purpose: maskable`` (macOS Chrome squircle / Android circle mask).

Colors: desktop forces ``.dark`` (Gradio Default dark body = #0f0f11);
mobile's fixed header bar is #1a1a2e — theme_color makes the standalone
window title bar / iOS status bar continue the page.

Icon files are resolved relative to __file__ (repo-root app_images/) —
file-move trap: moving this module to another depth breaks the path.
"""

import json
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response

_APP_IMAGES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app_images")

# Dictated by Gradio's template link (see module docstring / gradio_patches).
MANIFEST_FILENAME = "manifest.json"
ICON_SIZES = (192, 512)
ICON_ROUTE_PREFIX = "/static/pwa-icon-"

DESKTOP_THEME_COLOR = "#0f0f11"
MOBILE_THEME_COLOR = "#1a1a2e"


def icon_path(size: int) -> str:
    return os.path.join(_APP_IMAGES_DIR, f"pwa_icon_{size}.png")


def _icons() -> list:
    return [
        {
            "src": f"{ICON_ROUTE_PREFIX}{size}.png",
            "sizes": f"{size}x{size}",
            "type": "image/png",
            "purpose": purpose,
        }
        for purpose in ("any", "maskable")
        for size in ICON_SIZES
    ]


def build_desktop_manifest() -> dict:
    return {
        "id": "/",
        "name": "Artificial Girlfriend",
        "short_name": "Artificial Girlfriend",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": DESKTOP_THEME_COLOR,
        "theme_color": DESKTOP_THEME_COLOR,
        "icons": _icons(),
    }


def build_mobile_manifest(mobile_path: str = "/mobile") -> dict:
    scope = mobile_path.rstrip("/") + "/"
    return {
        "id": scope,
        "name": "AG Mobile",
        "short_name": "AG Mobile",
        "start_url": scope,
        "scope": scope,
        "display": "standalone",
        "background_color": MOBILE_THEME_COLOR,
        "theme_color": MOBILE_THEME_COLOR,
        "icons": _icons(),
    }


def _manifest_response(manifest: dict) -> Response:
    return Response(
        content=json.dumps(manifest, ensure_ascii=False, indent=2),
        media_type="application/manifest+json",
        # Revalidate on every visit so installed apps pick up name/icon changes.
        headers={"Cache-Control": "no-cache"},
    )


def register_pwa_routes(app: FastAPI, mobile_path: str = "/mobile") -> None:
    """Register the manifest + icon routes on the outer FastAPI app.

    Must be called BEFORE gr.mount_gradio_app(...) for "/" and mobile_path
    (Starlette matches routes in registration order; the "/" mount is a
    catch-all).
    """
    mobile_manifest_path = mobile_path.rstrip("/") + "/" + MANIFEST_FILENAME

    @app.get("/" + MANIFEST_FILENAME)
    async def desktop_manifest():
        return _manifest_response(build_desktop_manifest())

    @app.get(mobile_manifest_path)
    async def mobile_manifest():
        return _manifest_response(build_mobile_manifest(mobile_path))

    @app.get(ICON_ROUTE_PREFIX + "{size:int}.png")
    async def pwa_icon(size: int):
        if size not in ICON_SIZES:
            return Response(status_code=404)
        return FileResponse(
            icon_path(size),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )
