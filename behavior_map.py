"""
Map collar ``activity_level`` (accelerometer vibration magnitude) to a short label + emoji.

Three tiers — US-market copy for PawQuest Go (Central Park deployment).
"""

from __future__ import annotations

_STILL_BELOW = 2_200.0
_PATROL_BELOW = 22_000.0


def behavior_for_activity_level(activity_level: float) -> tuple[str, str]:
    """
    Return ``(label_en, emoji)``.

    - Still / low magnitude → Resting
    - Mid vibration → On patrol
    - High vibration → Zoomies
    """
    v = max(0.0, float(activity_level))
    if v < _STILL_BELOW:
        return "Resting", "💤"
    if v < _PATROL_BELOW:
        return "On patrol", "🐾"
    return "Zoomies", "🔥"


def behavior_for_vibration(vibration: float) -> tuple[str, str]:
    return behavior_for_activity_level(vibration)
