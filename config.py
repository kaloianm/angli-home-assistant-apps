"""Pure config models and parsing for ExtractorFanControl."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

DEFAULT_STAIRCASE_INTERVAL_SECONDS = 30
DEFAULT_PULSE_GUARD_SECONDS = 5
DEFAULT_MIN_LIGHT_ON_FOR_FAN_SECONDS = 15
DEFAULT_SHORT_VISIT_THRESHOLD_SECONDS = 60


@dataclass(frozen=True)
class PairConfig:
    """Configuration for one light/fan pair."""

    name: str
    light_entity: str
    fan_switch_entity: str
    min_light_on_for_fan_seconds: int
    short_visit_threshold_seconds: int
    daily_run_time: Optional[str]
    daily_run_duration_seconds: Optional[int]

    def __str__(self) -> str:
        """Human-friendly summary used in logs."""
        return ("PairConfig("
                f"name={self.name}, "
                f"light_entity={self.light_entity}, "
                f"fan_switch_entity={self.fan_switch_entity}, "
                "min_light_on_for_fan_seconds="
                f"{self.min_light_on_for_fan_seconds}, "
                "short_visit_threshold_seconds="
                f"{self.short_visit_threshold_seconds}, "
                f"daily_run_time={self.daily_run_time}, "
                "daily_run_duration_seconds="
                f"{self.daily_run_duration_seconds}"
                ")")


@dataclass(frozen=True)
class AppConfig:
    """Top-level ExtractorFanControl configuration."""

    staircase_interval_seconds: int
    pulse_guard_seconds: int
    pairs: List[PairConfig]

    @property
    def keepalive_pulse_interval_seconds(self) -> int:
        """Seconds between keepalive ON pulses."""
        return max(1, self.staircase_interval_seconds - self.pulse_guard_seconds)


def parse_app_config(args: Dict[str, Any]) -> AppConfig:
    """Parse and validate AppDaemon args for ExtractorFanControl."""
    staircase_interval_seconds = _parse_positive_int(args, "staircase_interval_seconds",
                                                     DEFAULT_STAIRCASE_INTERVAL_SECONDS)
    pulse_guard_seconds = _parse_non_negative_int(args, "pulse_guard_seconds",
                                                  DEFAULT_PULSE_GUARD_SECONDS)
    if pulse_guard_seconds >= staircase_interval_seconds:
        raise ValueError("pulse_guard_seconds must be smaller than staircase_interval_seconds")

    raw_pairs = args.get("pairs")
    if not isinstance(raw_pairs, list) or not raw_pairs:
        raise ValueError("pairs must be a non-empty list")

    pairs: List[PairConfig] = []
    seen_names = set()
    for idx, raw_pair in enumerate(raw_pairs):
        if not isinstance(raw_pair, dict):
            raise ValueError(f"pairs[{idx}] must be a mapping")
        pair = _parse_pair_config(raw_pair, idx)
        if pair.name in seen_names:
            raise ValueError(f"duplicate pair name: {pair.name}")
        seen_names.add(pair.name)
        pairs.append(pair)

    return AppConfig(
        staircase_interval_seconds=staircase_interval_seconds,
        pulse_guard_seconds=pulse_guard_seconds,
        pairs=pairs,
    )


def _parse_pair_config(raw_pair: Dict[str, Any], idx: int) -> PairConfig:
    """Parse and validate one pair object from args['pairs']."""
    light_entity = _require_non_empty_str(raw_pair, "light_entity", idx)
    fan_switch_entity = _require_non_empty_str(raw_pair, "fan_switch_entity", idx)

    raw_name = raw_pair.get("name")
    if raw_name is None or str(raw_name).strip() == "":
        name = f"pair_{idx}_{light_entity}_{fan_switch_entity}"
    else:
        name = str(raw_name).strip()

    min_light_on_for_fan_seconds = _parse_positive_int(
        raw_pair,
        "min_light_on_for_fan_seconds",
        DEFAULT_MIN_LIGHT_ON_FOR_FAN_SECONDS,
        idx=idx,
    )
    short_visit_threshold_seconds = _parse_positive_int(
        raw_pair,
        "short_visit_threshold_seconds",
        DEFAULT_SHORT_VISIT_THRESHOLD_SECONDS,
        idx=idx,
    )

    daily_run_time_raw = raw_pair.get("daily_run_time")
    daily_run_duration_raw = raw_pair.get("daily_run_duration_seconds")
    if daily_run_time_raw is None or daily_run_duration_raw is None:
        # Missing either field means this pair has no daily schedule configured.
        daily_run_time = None
        daily_run_duration_seconds = None
    else:
        daily_run_time = str(daily_run_time_raw).strip()
        if daily_run_time == "":
            raise ValueError(f"pairs[{idx}].daily_run_time is required")
        _validate_daily_time(daily_run_time, idx)
        daily_run_duration_seconds = _parse_positive_int(raw_pair,
                                                         "daily_run_duration_seconds",
                                                         default=None,
                                                         idx=idx)

    return PairConfig(
        name=name,
        light_entity=light_entity,
        fan_switch_entity=fan_switch_entity,
        min_light_on_for_fan_seconds=min_light_on_for_fan_seconds,
        short_visit_threshold_seconds=short_visit_threshold_seconds,
        daily_run_time=daily_run_time,
        daily_run_duration_seconds=daily_run_duration_seconds,
    )


def _validate_daily_time(value: str, idx: int) -> None:
    """Validate daily time format HH:MM."""
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise ValueError(f"pairs[{idx}].daily_run_time must be HH:MM") from exc


def _require_non_empty_str(source: Dict[str, Any], key: str, idx: Optional[int] = None) -> str:
    """Read a required non-empty string field."""
    value = source.get(key)
    if value is None or str(value).strip() == "":
        where = f"pairs[{idx}].{key}" if idx is not None else key
        raise ValueError(f"{where} is required")
    return str(value).strip()


def _parse_positive_int(source: Dict[str, Any],
                        key: str,
                        default: Optional[int],
                        idx: Optional[int] = None) -> int:
    """Read integer field and enforce > 0."""
    value = source.get(key, default)
    where = f"pairs[{idx}].{key}" if idx is not None else key
    if value is None:
        raise ValueError(f"{where} is required")
    try:
        value_int = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{where} must be an integer") from exc
    if value_int <= 0:
        raise ValueError(f"{where} must be > 0")
    return value_int


def _parse_non_negative_int(source: Dict[str, Any],
                            key: str,
                            default: int,
                            idx: Optional[int] = None) -> int:
    """Read integer field and enforce >= 0."""
    value = source.get(key, default)
    where = f"pairs[{idx}].{key}" if idx is not None else key
    try:
        value_int = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{where} must be an integer") from exc
    if value_int < 0:
        raise ValueError(f"{where} must be >= 0")
    return value_int
