"""
Pure config models and parsing for ExtractorFanControl.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ExtractorFanConfig:
    """
    Configuration for one light/fan pair.
    """

    name: str
    light_entity: str
    fan_switch_entity: str
    off_at_sensor_entity: str
    min_light_on_for_fan_seconds: int
    short_visit_threshold_seconds: int
    daily_run_time: str | None
    daily_run_duration_seconds: int | None
    staircase_interval_seconds: int
    pulse_guard_seconds: int

    @property
    def keepalive_pulse_interval_seconds(self) -> int:
        """
        Seconds between keepalive ON pulses.
        """
        return max(1, self.staircase_interval_seconds - self.pulse_guard_seconds)

    def __str__(self) -> str:
        """
        Human-friendly summary used in logs.
        """
        return ("ExtractorFanConfig("
                f"name={self.name}, "
                f"light_entity={self.light_entity}, "
                f"fan_switch_entity={self.fan_switch_entity}, "
                f"off_at_sensor_entity={self.off_at_sensor_entity}, "
                f"min_light_on_for_fan_seconds={self.min_light_on_for_fan_seconds}, "
                f"short_visit_threshold_seconds={self.short_visit_threshold_seconds}, "
                f"daily_run_time={self.daily_run_time}, "
                f"daily_run_duration_seconds={self.daily_run_duration_seconds}, "
                f"staircase_interval_seconds={self.staircase_interval_seconds}, "
                f"pulse_guard_seconds={self.pulse_guard_seconds}"
                ")")


def parse_app_config(args: dict[str, Any]) -> ExtractorFanConfig:
    """
    Parse and validate AppDaemon args for one ExtractorFanControl instance.
    """
    staircase_interval_seconds = _parse_positive_int(args, "staircase_interval_seconds")
    pulse_guard_seconds = _parse_non_negative_int(args, "pulse_guard_seconds")
    if pulse_guard_seconds >= staircase_interval_seconds:
        raise ValueError("pulse_guard_seconds must be smaller than staircase_interval_seconds")

    name = _require_non_empty_str(args, "name")
    light_entity = _require_non_empty_str(args, "light_entity")
    fan_switch_entity = _require_non_empty_str(args, "fan_switch_entity")

    if args.get("off_at_sensor_entity") is None:
        off_at_sensor_entity = f"sensor.extractor_fan_{name}_off_at"
    else:
        off_at_sensor_entity = _require_non_empty_str(args, "off_at_sensor_entity")
        if not off_at_sensor_entity.startswith("sensor."):
            raise ValueError("off_at_sensor_entity must start with 'sensor.'")

    min_light_on_for_fan_seconds = _parse_non_negative_int(args, "min_light_on_for_fan_seconds")
    short_visit_threshold_seconds = _parse_positive_int(args, "short_visit_threshold_seconds")
    if min_light_on_for_fan_seconds > short_visit_threshold_seconds:
        raise ValueError("min_light_on_for_fan_seconds must be <= short_visit_threshold_seconds")

    daily_run_time_raw = args.get("daily_run_time")
    daily_run_duration_raw = args.get("daily_run_duration_seconds")
    if daily_run_time_raw is None or daily_run_duration_raw is None:
        daily_run_time = None
        daily_run_duration_seconds = None
    else:
        daily_run_time = str(daily_run_time_raw).strip()
        if daily_run_time == "":
            raise ValueError("daily_run_time is required")
        _validate_daily_time(daily_run_time)
        daily_run_duration_seconds = _parse_positive_int(args, "daily_run_duration_seconds")

    return ExtractorFanConfig(
        name=name,
        light_entity=light_entity,
        fan_switch_entity=fan_switch_entity,
        off_at_sensor_entity=off_at_sensor_entity,
        min_light_on_for_fan_seconds=min_light_on_for_fan_seconds,
        short_visit_threshold_seconds=short_visit_threshold_seconds,
        daily_run_time=daily_run_time,
        daily_run_duration_seconds=daily_run_duration_seconds,
        staircase_interval_seconds=staircase_interval_seconds,
        pulse_guard_seconds=pulse_guard_seconds,
    )


def _validate_daily_time(value: str) -> None:
    """
    Validate daily time format HH:MM.
    """
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise ValueError("daily_run_time must be HH:MM") from exc


def _require_non_empty_str(source: dict[str, Any], key: str) -> str:
    """
    Read a required non-empty string field.
    """
    value = source.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"{key} is required")
    return str(value).strip()


def _parse_int(source: dict[str, Any], key: str) -> int:
    """
    Read a required integer field.
    """
    value = source.get(key)
    if value is None:
        raise ValueError(f"{key} is required")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc


def _parse_positive_int(source: dict[str, Any], key: str) -> int:
    """
    Read a required integer field and enforce > 0.
    """
    value = _parse_int(source, key)
    if value <= 0:
        raise ValueError(f"{key} must be > 0")
    return value


def _parse_non_negative_int(source: dict[str, Any], key: str) -> int:
    """
    Read a required integer field and enforce >= 0.
    """
    value = _parse_int(source, key)
    if value < 0:
        raise ValueError(f"{key} must be >= 0")
    return value
