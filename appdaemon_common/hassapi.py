"""
AppDaemon's ``Hass`` base class, or a bare stand-in when AppDaemon is not installed, so the apps
import and run their tests without it.
"""

__all__ = ["Hass"]

try:
    from appdaemon.plugins.hass.hassapi import Hass
except ImportError:  # pragma: no cover - used only outside AppDaemon runtime.

    class Hass:  # type: ignore[no-redef]
        """
        Stand-in for ``appdaemon.plugins.hass.hassapi.Hass``.
        """
