"""
Pure config models and parsing for GradhermeticCoverControl.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from gradhermetic_cover_control.logic import POSITION_TOLERANCE_PCT


@dataclass(frozen=True)
class GradhermeticConfig:
    """
    Configuration for one Gradhermetic virtual cover.
    """

    real_cover: str
    virtual_id: str
    virtual_name: str
    tilt_zone_upper_pct: float
    tilt_zone_lower_pct: float
    tilt_zone_epsilon_pct: float
    tilt_step_pct: float
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
                f"tilt_zone_upper_pct={self.tilt_zone_upper_pct}, "
                f"tilt_zone_lower_pct={self.tilt_zone_lower_pct}, "
                f"tilt_zone_epsilon_pct={self.tilt_zone_epsilon_pct}, "
                f"tilt_step_pct={self.tilt_step_pct}, "
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

    tilt_zone_upper_pct = _parse_percentage(args, "tilt_zone_upper_pct")
    tilt_zone_lower_pct = _parse_percentage(args, "tilt_zone_lower_pct")
    if tilt_zone_lower_pct >= tilt_zone_upper_pct:
        raise ValueError("tilt_zone_lower_pct must be smaller than tilt_zone_upper_pct")

    tilt_zone_epsilon_pct = _parse_positive_float(args, "tilt_zone_epsilon_pct")
    if tilt_zone_epsilon_pct <= POSITION_TOLERANCE_PCT:
        raise ValueError(
            f"tilt_zone_epsilon_pct must be > {POSITION_TOLERANCE_PCT} (the position-feedback "
            "tolerance) so the dip and leave margins reliably clear the zone edges")
    if tilt_zone_lower_pct - tilt_zone_epsilon_pct < 0.0:
        raise ValueError("tilt_zone_lower_pct - tilt_zone_epsilon_pct must be >= 0")
    if tilt_zone_upper_pct + tilt_zone_epsilon_pct > 100.0:
        raise ValueError("tilt_zone_upper_pct + tilt_zone_epsilon_pct must be <= 100")

    tilt_step_pct = _parse_positive_float(args, "tilt_step_pct")
    min_step_pct = 100.0 / (tilt_zone_upper_pct - tilt_zone_lower_pct)
    if tilt_step_pct < min_step_pct:
        raise ValueError(
            f"tilt_step_pct must be >= {min_step_pct:.2f} so one step moves the actuator at least "
            f"one reported percent within the {tilt_zone_upper_pct - tilt_zone_lower_pct:.0f}% "
            "tilt zone")

    knx_move_address = _optional_str(args, "knx_move_address")
    knx_step_address = _optional_str(args, "knx_step_address")

    return GradhermeticConfig(
        real_cover=real_cover,
        virtual_id=virtual_id,
        virtual_name=virtual_name,
        tilt_zone_upper_pct=tilt_zone_upper_pct,
        tilt_zone_lower_pct=tilt_zone_lower_pct,
        tilt_zone_epsilon_pct=tilt_zone_epsilon_pct,
        tilt_step_pct=tilt_step_pct,
        knx_move_address=knx_move_address,
        knx_step_address=knx_step_address,
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
