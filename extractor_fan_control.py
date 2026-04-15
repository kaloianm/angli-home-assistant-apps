"""AppDaemon entry point for ExtractorFanControl (Phase 2).

This module handles app argument parsing and runtime scaffolding only.
Event listeners, timer orchestration, and logic wiring are intentionally
deferred to Phase 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from extractor_fan_control.config import PairConfig, parse_app_config

try:
    import appdaemon.plugins.hass.hassapi as hass
except ImportError:  # pragma: no cover - used only outside AppDaemon runtime.

    class _HassBase:
        pass

    class hass:  # type: ignore[no-redef]
        Hass = _HassBase


@dataclass
class PairRuntime:
    """Mutable runtime scaffolding for one pair.

    Listener and timer handles remain placeholders in Phase 2 and will be used
    when wiring logic and callbacks in Phase 3.
    """

    config: PairConfig
    logic: Optional[Any] = None
    light_listener_handle: Optional[Any] = None
    fan_listener_handle: Optional[Any] = None
    activation_timer_handle: Optional[Any] = None
    deadline_timer_handle: Optional[Any] = None
    keepalive_timer_handle: Optional[Any] = None
    daily_schedule_handle: Optional[Any] = None


class ExtractorFanControl(hass.Hass):
    """AppDaemon app shell for extractor fan control.

    Phase 2 responsibilities:
    - parse and validate args from apps.yaml
    - create per-pair runtime placeholders
    - expose structured config/runtime for Phase 3 wiring
    """

    def initialize(self) -> None:
        """AppDaemon startup hook."""
        # AppDaemon convention initializes instance state in this hook.
        # pylint: disable=attribute-defined-outside-init
        self._config = parse_app_config(self.args or {})
        self._runtime_by_name: Dict[str, PairRuntime] = {}
        self._runtime_by_light_entity: Dict[str, PairRuntime] = {}
        self._runtime_by_fan_entity: Dict[str, PairRuntime] = {}

        for pair_config in self._config.pairs:
            runtime = PairRuntime(config=pair_config)
            self._runtime_by_name[pair_config.name] = runtime
            self._runtime_by_light_entity[pair_config.light_entity] = runtime
            self._runtime_by_fan_entity[
                pair_config.fan_switch_entity] = runtime

        self.log("ExtractorFanControl initialized with "
                 f"{len(self._runtime_by_name)} pair(s). "
                 "keepalive_pulse_interval_seconds="
                 f"{self._config.keepalive_pulse_interval_seconds}")
