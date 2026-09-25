"""
backend/shared/platform_caps.py

Single source of truth for OS detection and platform capability flags
(Mac port plan 0-1, 2026-07-20 — internal design doc, not part of the repository).

Rules:
- Feature availability checks use UNSUPPORTED_FEATURES, never a raw
  sys.platform test scattered at call sites.
- Prompt content must NOT branch on OS (golden byte contract); branch on
  feature flags instead (plan §9-2).
"""

import sys

IS_MAC = sys.platform == 'darwin'
IS_WINDOWS = sys.platform == 'win32'

# Feature keys unavailable on the current platform.
# 'command_execution' = NirCMD/CMD-based PC command feature (Mac skip,
# 2026-07-20 稜裁定 A).
UNSUPPORTED_FEATURES: frozenset = frozenset(
    {'command_execution'} if IS_MAC else set()
)


def is_feature_supported(feature_key: str) -> bool:
    """Return True if the feature is available on this platform."""
    return feature_key not in UNSUPPORTED_FEATURES
