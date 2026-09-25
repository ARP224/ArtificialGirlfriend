"""
Artificial Girlfriend UI Package

This package contains the user interface components for the Artificial Girlfriend application.
The UI is built using Gradio and is organized into modular components.

Modules:
    - state: Application state management
    - components: UI component builders
    - conversation: Conversation and audio handling
    - character_ui: Character management functionality
    - error_handler: Generic error handling utilities
    - app: Main application entry point
"""

# Import main entry points for easy access
from .app import initialize_application, shutdown_application, run_ui, main

# Export public API
__all__ = [
    'initialize_application',
    'shutdown_application',
    'run_ui',
    'main'
]