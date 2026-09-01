"""
Pure config models and parsing for GradhermeticCoverControl.

This module only reads and type-checks the ``apps.yaml`` args. Every rule relating the tilt-zone
numbers to each other lives in :mod:`geometry`, which this module delegates to by constructing a
:class:`~gradhermetic_cover_control.geometry.Zone`.

Every percentage in the config is **real blind travel** -- the numbers the actuator reports and
accepts. The inverted virtual slat scale exists only inside the app (it is what the cover entity's
position slider shows while tilt mode is engaged), and :class:`Zone` is the only place the two
meet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from gradhermetic_cover_control.geometry import Zone


@dataclass(frozen=True)
class GradhermeticConfig:
    """
    Configuration for one Gradhermetic virtual cover.
    """

    real_cover: str
    virtual_id: str
    virtual_name: str
    zone: Zone
    knx_move_address: Optional[str]
    knx_step_address: Optional[str]

    def __str__(self) -> str:
        """
        Human-friendly summary used in logs.
        """
        return ("GradhermeticConfig("
                f"real_cover={self.real_cover}, "
                f"virtual_id={self.virtual_id}, "
                f"virtual_name={self.virtual_name}, "
                f"tilt_zone_upper_pct={self.zone.tilt_zone_upper_pct}, "
                f"tilt_zone_lower_pct={self.zone.tilt_zone_lower_pct}, "
                f"tilt_zone_epsilon_pct={self.zone.tilt_zone_epsilon_pct}, "
                f"tilt_zone_release_pct={self.zone.release_target}, "
                f"tilt_enter_landing_pct={self.zone.enter_landing_real}, "
                f"tilt_step_pct={self.zone.tilt_step_pct}, "
                f"knx_move_address={self.knx_move_address}, "
                f"knx_step_address={self.knx_step_address}"
                ")")


def parse_app_config(args: Dict[str, Any]) -> GradhermeticConfig:
    """
    Parse and validate AppDaemon args for one GradhermeticCoverControl instance.
    """
    real_cover = _require_non_empty_str(args, "real_cover")
    virtual_id = _require_non_empty_str(args, "virtual_id")
    virtual_name = _require_non_empty_str(args, "virtual_name")

    # Read each number in isolation here (present, numeric, in range); Zone owns every rule that
    # relates them to one another and raises naming the same apps.yaml keys.
    zone = Zone(
        tilt_zone_upper_pct=_parse_percentage(args, "tilt_zone_upper_pct"),
        tilt_zone_lower_pct=_parse_percentage(args, "tilt_zone_lower_pct"),
        tilt_zone_epsilon_pct=_parse_positive_float(args, "tilt_zone_epsilon_pct"),
        tilt_step_pct=_parse_positive_float(args, "tilt_step_pct"),
        # Both optional: absent means "keep the geometric default", which Zone supplies.
        tilt_zone_release_pct=_optional_percentage(args, "tilt_zone_release_pct"),
        tilt_enter_landing_pct=_optional_percentage(args, "tilt_enter_landing_pct"),
    )

    return GradhermeticConfig(
        real_cover=real_cover,
        virtual_id=virtual_id,
        virtual_name=virtual_name,
        zone=zone,
        knx_move_address=_optional_str(args, "knx_move_address"),
        knx_step_address=_optional_str(args, "knx_step_address"),
    )


def _require_non_empty_str(source: Dict[str, Any], key: str) -> str:
    """
    Read a required non-empty string field.
    """
    value = source.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"{key} is required")
    return str(value).strip()


def _optional_str(source: Dict[str, Any], key: str) -> Optional[str]:
    """
    Read an optional string field, returning None when absent or blank.
    """
    value = source.get(key)
    if value is None or str(value).strip() == "":
        return None
    return str(value).strip()


def _parse_percentage(source: Dict[str, Any], key: str) -> float:
    """
    Read a float field and enforce 0 <= value <= 100.
    """
    value = _parse_float(source, key)
    if value < 0.0 or value > 100.0:
        raise ValueError(f"{key} must be between 0 and 100")
    return value


def _optional_percentage(source: Dict[str, Any], key: str) -> Optional[float]:
    """
    Read an optional float field, enforcing 0 <= value <= 100 when it is present.

    Absent (or explicitly null) means the caller keeps its own default; anything relating the value
    to the rest of the geometry is Zone's business.
    """
    if source.get(key) is None:
        return None
    return _parse_percentage(source, key)


def _parse_positive_float(source: Dict[str, Any], key: str) -> float:
    """
    Read a float field and enforce > 0.
    """
    value = _parse_float(source, key)
    if value <= 0.0:
        raise ValueError(f"{key} must be > 0")
    return value


def _parse_float(source: Dict[str, Any], key: str) -> float:
    """
    Read a required numeric field as float.
    """
    value = source.get(key)
    if value is None:
        raise ValueError(f"{key} is required")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc
