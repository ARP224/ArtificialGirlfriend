"""
launcher package — tray launcher and its leaf modules.

Runs outside the app's layer structure (separate process). App code may
import stdlib-only leaves from here (startup_registry); nothing in this
package imports backend/ui.
"""
