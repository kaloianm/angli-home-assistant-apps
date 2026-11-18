"""
Config parsing for GradhermeticCoverControl.

Reads and type-checks the ``apps.yaml`` args; rules relating the tilt-zone numbers to each other
live in :class:`~gradhermetic_cover_control.geometry.Zone`. Percentages must be whole: ``38.5`` is
rejected rather than silently rounding a calibrated edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
                f"tilt_zone_release_pct={self.zone.tilt_zone_release_pct}, "
                f"tilt_zone_enter_pct={self.zone.enter_landing_real}, "
                f"tilt_step_pct={self.zone.tilt_step_pct}, "
                f"height_step_pct={self.zone.height_step_pct}"
                ")")


def parse_app_config(args: dict[str, Any]) -> GradhermeticConfig:
    """
    Parse and validate AppDaemon args for one GradhermeticCoverControl instance.
    """
    real_cover = _require_non_empty_str(args, "real_cover")
    virtual_id = _require_non_empty_str(args, "virtual_id")
    virtual_name = _require_non_empty_str(args, "virtual_name")

    zone = Zone(
        tilt_zone_upper_pct=_parse_percentage(args, "tilt_zone_upper_pct"),
        tilt_zone_lower_pct=_parse_percentage(args, "tilt_zone_lower_pct"),
        tilt_zone_epsilon_pct=_parse_positive_whole(args, "tilt_zone_epsilon_pct"),
        tilt_zone_release_pct=_parse_percentage(args, "tilt_zone_release_pct"),
        tilt_step_pct=_parse_positive_whole(args, "tilt_step_pct"),
        tilt_zone_enter_pct=_parse_percentage(args, "tilt_zone_enter_pct"),
        height_step_pct=_parse_positive_whole(args, "height_step_pct"),
    )

    return GradhermeticConfig(
        real_cover=real_cover,
        virtual_id=virtual_id,
        virtual_name=virtual_name,
        zone=zone,
    )


def _require_non_empty_str(source: dict[str, Any], key: str) -> str:
    """
    Read a required non-empty string field.
    """
    value = source.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"{key} is required")
    return str(value).strip()


def _parse_percentage(source: dict[str, Any], key: str) -> int:
    """
    Read a whole-number field and enforce 0 <= value <= 100.
    """
    value = _parse_whole(source, key)
    if value < 0 or value > 100:
        raise ValueError(f"{key} must be between 0 and 100")
    return value


def _parse_positive_whole(source: dict[str, Any], key: str) -> int:
    """
    Read a whole-number field and enforce > 0.
    """
    value = _parse_whole(source, key)
    if value <= 0:
        raise ValueError(f"{key} must be > 0")
    return value


def _parse_whole(source: dict[str, Any], key: str) -> int:
    """
    Read a required numeric field as a whole number.

    Integers, integral floats (YAML reads ``38.0`` as one) and numeric strings are accepted; a
    fractional value is an error, since no actuator position corresponds to it.
    """
    value = source.get(key)
    if value is None:
        raise ValueError(f"{key} is required")
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc
    if not number.is_integer():
        raise ValueError(f"{key} must be a whole percent, not {value!r}")
    return int(number)
