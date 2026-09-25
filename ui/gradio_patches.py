"""
ui/gradio_patches.py

Runtime compatibility patch for gradio_client 1.7.0 (the pinned pair of
gradio 5.15.0).

gradio_client's json-schema helpers crash with
``TypeError: argument of type 'bool' is not iterable`` when a component
schema contains a boolean sub-schema (e.g. ``additionalProperties: true``,
valid JSON Schema / OpenAPI 3.1). gradio's ``get_api_info()`` hits this on
this app's components, which 500s the main page; launch()'s localhost
self-check then also fails and surfaces the misleading
"When localhost is not accessible ..." dialog.

Until 2026-07-06 this fix lived as a hand edit inside the old venv's
site-packages — invisible to ``pip freeze`` and silently lost on venv
rebuild (the rename-incident). Keeping it here in the repo survives any
venv rebuild. Remove once gradio is upgraded to a release whose client
handles boolean schemas upstream.
"""

import functools
import logging
import re

logger = logging.getLogger(__name__)

# Gradio 5.15 bakes external references into templates/frontend/index.html:
# preconnect hints to fonts.googleapis.com / fonts.gstatic.com (TLS handshake
# to Google even though our themes no longer fetch any Google Font) and an
# iframe-resizer helper script from cdnjs.cloudflare.com (only functional when
# the app is embedded in an iframe — AG never is). AG policy: no external
# communication without a clear purpose(稜裁定 2026-07-19) — strip all three
# from the served HTML. `[^>]*` also spans newlines (the script tag is
# multi-line in the template).
_EXTERNAL_REF_RE = re.compile(
    r'<link[^>]*fonts\.g(?:oogleapis|static)\.com[^>]*/?>'
    r'|<script[^>]*cdnjs\.cloudflare\.com[^>]*>\s*</script>'
)

# Gradio's template hardcodes `<link rel="manifest" href="/manifest.json">`
# (root-absolute) on every page, so a page mounted at /mobile/ would fetch
# the "/" app's manifest. Rewriting the href to the mount-relative
# `manifest.json` lets each mount reference its own: "/" -> /manifest.json,
# /mobile/ -> /mobile/manifest.json (served by ui/pwa_manifest.py in server
# mode; elsewhere Gradio's own pwa=False handler answers 404 as before).
_MANIFEST_HREF_RE = re.compile(r'href="/manifest\.json"')
_MANIFEST_HREF_RELATIVE = 'href="manifest.json"'


def apply_gradio_client_patches() -> None:
    """Make gradio_client's schema helpers tolerate boolean sub-schemas."""
    from gradio_client import utils as client_utils

    if getattr(client_utils, '_ag_bool_schema_patched', False):
        return

    orig_get_type = client_utils.get_type
    orig_to_python_type = client_utils._json_schema_to_python_type

    @functools.wraps(orig_get_type)
    def get_type(schema):
        if isinstance(schema, bool):
            return "boolean"
        if not isinstance(schema, dict):
            return "any"
        return orig_get_type(schema)

    @functools.wraps(orig_to_python_type)
    def _json_schema_to_python_type(schema, defs):
        # ``true`` allows anything, ``false`` allows nothing (JSON Schema).
        if isinstance(schema, bool):
            return "Any" if schema else "Never"
        if not isinstance(schema, dict):
            return "Any"
        return orig_to_python_type(schema, defs)

    # Module-level rebinding also covers the recursive calls inside
    # gradio_client (they resolve these names via module globals).
    client_utils.get_type = get_type
    client_utils._json_schema_to_python_type = _json_schema_to_python_type
    client_utils._ag_bool_schema_patched = True
    logger.info("Applied gradio_client bool-schema compatibility patch")


def apply_gradio_local_font_patch() -> None:
    """Drop LocalFont @font-face rules whose woff2 does not actually exist.

    gradio 5.15 emits an @font-face for EVERY LocalFont in a theme stack,
    pointing at static/fonts/<Name>/<Name>-{Regular,Bold}.woff2 with no
    existence check (themes/utils/fonts.py). Only its bundled families
    (IBMPlexMono, Montserrat, ...) resolve; the rest 404. Worse than the
    404 noise: a broken face SHADOWS the family name, so 'Source Sans 3'
    (our data-URI @font-face in LOCAL_FONTS_CSS) loses to gradio's broken
    duplicate and text degrades to the browser default, and generic-ish
    entries like 'ui-sans-serif' / 'Consolas' stop resolving to real
    system fonts (稜HAR+スクショ実測 2026-07-19). Filtering rules by
    file existence keeps the working bundled fonts and removes only the
    poison. Must run before any Blocks is created (theme css is built at
    Blocks init).
    """
    import os

    import gradio
    from gradio.themes.utils import fonts

    if getattr(fonts, '_ag_localfont_patched', False):
        return

    static_fonts = os.path.join(
        os.path.dirname(gradio.__file__), 'templates', 'frontend', 'static', 'fonts'
    )
    orig_stylesheet = fonts.LocalFont.stylesheet
    rule_re = re.compile(r'@font-face\s*\{[^}]*\}')
    url_re = re.compile(r"url\('static/fonts/([^']+)'\)")

    def stylesheet(self):
        result = orig_stylesheet(self)
        css = result.get('css') if isinstance(result, dict) else None
        if not css:
            return result
        kept = []
        for rule in rule_re.findall(css):
            m = url_re.search(rule)
            if m and os.path.isfile(os.path.join(static_fonts, *m.group(1).split('/'))):
                kept.append(rule)
        return {'url': result.get('url'), 'css': '\n'.join(kept) or None}

    fonts.LocalFont.stylesheet = stylesheet
    fonts._ag_localfont_patched = True
    logger.info("Applied gradio LocalFont dead-@font-face filter patch")


# gradio 5.15 Tabs bundle: the overflow calculation walks tab buttons from
# the END and a hidden trailing tab (it renders no button, so it has no
# measured rect) falls into overflow_tabs; the "..." dropdown template has
# no visibility filter, so a Tab hidden via gr.update(visible=False)
# resurfaces as a dead entry in the overflow menu (History page YouTube tab
# — 稜実機 2026-07-20). Filtering overflow_tabs by tab visibility fixes the
# ghost entry AND the needless "..." button (is_overflowing counts the same
# list). The target string is the exact minified form of the pinned build's
# `overflow_tabs = tabs.slice(last_visible_index + 1)` (js/tabs/shared/
# Tabs.svelte handle_menu_overflow — recovered from the shipped .js.map).
_TABS_OVERFLOW_TARGET = "l(8,T=b.slice(W+1))"
_TABS_OVERFLOW_FIXED = "l(8,T=b.slice(W+1).filter(t=>t&&t.visible))"


def apply_gradio_tabs_overflow_patch() -> None:
    """Serve a Tabs bundle where hidden tabs never enter the overflow menu.

    site-packages stays pristine (the pip-freeze rename-incident lesson):
    a patched copy lives in the temp dir and ``routes_safe_join`` — resolved
    via module globals by the ``/assets/{path}`` route at call time — is
    redirected for exactly that one file. On a gradio upgrade the minified
    target vanishes and this becomes a logged no-op (re-check upstream:
    hidden tabs in the overflow dropdown may be fixed there).
    """
    import tempfile
    from pathlib import Path

    import gradio
    from gradio import routes

    if getattr(routes, '_ag_tabs_overflow_patched', False):
        return

    assets_dir = Path(gradio.__file__).parent / 'templates' / 'frontend' / 'assets'
    original = None
    for candidate in assets_dir.glob('Tabs-*.js'):
        if _TABS_OVERFLOW_TARGET in candidate.read_text(encoding='utf-8'):
            original = candidate
            break
    if original is None:
        logger.warning(
            "gradio Tabs overflow patch: minified target not found "
            "(gradio upgraded?) — hidden tabs will show as dead overflow entries"
        )
        return

    patched = Path(tempfile.gettempdir()) / 'ag_gradio_patches' / original.name
    content = original.read_text(encoding='utf-8').replace(
        _TABS_OVERFLOW_TARGET, _TABS_OVERFLOW_FIXED
    )
    patched.parent.mkdir(parents=True, exist_ok=True)
    if not patched.exists() or patched.read_text(encoding='utf-8') != content:
        patched.write_text(content, encoding='utf-8')

    orig_safe_join = routes.routes_safe_join
    original_str = str(original)

    @functools.wraps(orig_safe_join)
    def routes_safe_join(directory, path):
        result = orig_safe_join(directory, path)
        if result == original_str:
            return str(patched)
        return result

    routes.routes_safe_join = routes_safe_join
    routes._ag_tabs_overflow_patched = True
    logger.info("Applied gradio Tabs overflow visibility patch")


def build_locale_head_script() -> str:
    """Inline script that pins Gradio's built-in UI strings to the app language.

    Gradio 5 resolves its frontend locale from ``window.navigator.language``
    (svelte-i18n), so built-in labels like the image-upload
    「画像をここにドロップ」 follow the *browser* language and ignore the
    app's UI-language setting (稜指摘 2026-07-19: 英語UIでも日本語表示)。
    Shadowing the navigator getters before Gradio's module bundle evaluates
    makes its locale init see the app language instead.

    NOTE: this must reach the browser inside the *served HTML's* <head>
    (see apply_gradio_index_patch). gr.Blocks(head=...) does NOT work for
    this: Gradio injects head= content from the config on the client side,
    after the bundle has already initialized i18n(稜実踏 2026-07-19).
    """
    from backend.shared.i18n import current_language
    lang = current_language()
    return (
        '<script>(function(){'
        f'var l="{lang}";'
        'try{'
        'Object.defineProperty(window.navigator,"language",'
        '{get:function(){return l;},configurable:true});'
        'Object.defineProperty(window.navigator,"languages",'
        '{get:function(){return [l];},configurable:true});'
        '}catch(e){}'
        '})();</script>'
    )


def apply_gradio_index_patch() -> None:
    """Serve Gradio's index.html adjusted for AG: locale pin + external-ref strip
    + mount-relative manifest link.

    Three adjustments to the served page:
    1. Inject the locale-pinning script right after <head> — the Jinja
       template (gradio/templates/frontend/index.html) has no head
       placeholder and custom head= lands client-side too late (docstring
       above). Inline scripts run during HTML parse, before the deferred
       <script type="module"> bundle evaluates its i18n init.
    2. Remove Gradio's baked-in external references (_EXTERNAL_REF_RE):
       Google Fonts preconnects + cdnjs iframe-resizer. If AG is ever
       embedded in an iframe, re-add iframe-resizer as a bundled copy
       instead of the CDN reference.
    3. Make the Web App Manifest link mount-relative (_MANIFEST_HREF_RE) so
       each mounted page references its own manifest — the installable
       web app support in ui/pwa_manifest.py depends on this. (head= cannot
       add a second manifest link: browsers honour only the first one, and
       the template's comes first.)

    Covers every mounted app (desktop / mobile / admin) since gradio.routes
    shares one module-global Jinja environment.
    """
    import jinja2
    from gradio import routes

    env = routes.templates.env
    if getattr(env, '_ag_index_patched', False):
        return
    orig_loader = env.loader

    class _IndexRewriteLoader(jinja2.BaseLoader):
        def get_source(self, environment, template):
            source, filename, uptodate = orig_loader.get_source(environment, template)
            if template.endswith('index.html'):
                # {% raw %} so Jinja never parses the JS braces.
                source = source.replace(
                    '<head>',
                    '<head>{% raw %}' + build_locale_head_script() + '{% endraw %}',
                    1,
                )
                source = _EXTERNAL_REF_RE.sub('', source)
                source = _MANIFEST_HREF_RE.sub(_MANIFEST_HREF_RELATIVE, source)
            return source, filename, uptodate

    env.loader = _IndexRewriteLoader()
    try:
        env.cache.clear()
    except AttributeError:
        pass
    env._ag_index_patched = True
    logger.info("Applied gradio index.html rewrite patch (locale + external-ref strip + relative manifest link)")
