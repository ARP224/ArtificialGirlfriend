"""Self-hosted UI font delivery (Source Sans 3).

AG policy: no external communication without a clear purpose. Gradio's
Default theme injects a fonts.googleapis.com stylesheet link; instead we
embed the Adobe-official Source Sans 3 woff2 files (fonts/ at repo root,
unmodified — see fonts/README.md for provenance and OFL license) as
base64 data URIs in an @font-face block, so every UI (desktop / admin /
mobile / companion) renders the same typeface with zero runtime network
access. Delivery via css= keeps one code path for both local and server
modes (no static routes, no allowed_paths).

Fonts are resolved relative to __file__ (repo-root fonts/) — file-move
trap: moving this module to another directory depth breaks the path.
"""

import base64
import logging
import os

logger = logging.getLogger(__name__)

_FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fonts")

# (filename, css font-weight) — matches what the Google-hosted Default theme
# used to load (400/600 only, no italics; browsers closest-match bold/italic
# from these, same as before the self-hosting switch).
_FACES = [
    ("SourceSans3-Regular.ttf.woff2", 400),
    ("SourceSans3-Semibold.ttf.woff2", 600),
]

# Must match the first entry of the theme font stacks in app.py /
# admin_app.py / mobile_app.py.
FONT_FAMILY = "Source Sans 3"


def _build_css() -> str:
    rules = []
    for filename, weight in _FACES:
        path = os.path.join(_FONTS_DIR, filename)
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except OSError as e:
            # Graceful degrade: missing font file must not kill the UI —
            # the theme stack falls back to ui-sans-serif / system-ui.
            logger.warning("Local font unavailable (%s): %s", filename, e)
            continue
        rules.append(
            "@font-face {\n"
            f"    font-family: '{FONT_FAMILY}';\n"
            "    font-style: normal;\n"
            f"    font-weight: {weight};\n"
            "    font-display: swap;\n"
            f"    src: url(data:font/woff2;base64,{b64}) format('woff2');\n"
            "}\n"
        )
    return "".join(rules)


# Built once at import; ~300KB of CSS shared by all Blocks in this process.
LOCAL_FONTS_CSS = _build_css()
