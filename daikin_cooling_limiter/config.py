"""
Pure config models and parsing for DaikinACControl.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass(frozen=True)
class Settings:
    """
    Temperature hysteresis thresholds for AC control decisions.
    """

    off_hysteresis: float
    on_hysteresis: float


@dataclass(frozen=True)
class AppConfig:
    """
    Top-level DaikinACControl configuration.
    """

    ac_mode_entity: str
    ac_entities: List[str]
    settings: Settings


def parse_app_config(args: Dict[str, Any]) -> AppConfig:
    """
    Parse and validate AppDaemon args for DaikinACControl.
    """
    ac_mode_entity = _parse_ac_mode(args)
    ac_entities = _parse_ac_entities(args)

    raw_settings = args.get("settings")
    if not isinstance(raw_settings, dict):
        raise ValueError("settings must be a mapping")
    settings = _parse_settings(raw_settings)

    return AppConfig(
        ac_mode_entity=ac_mode_entity,
        ac_entities=ac_entities,
        settings=settings,
    )


def _parse_ac_mode(args: Dict[str, Any]) -> str:
    """
    Parse ac_mode as a string or single-element list.
    """
    raw = args.get("ac_mode")
    if raw is None:
        raise ValueError("ac_mode is required")
    if isinstance(raw, list):
        if len(raw) != 1:
            raise ValueError("ac_mode must contain exactly one entity")
        entity = str(raw[0]).strip()
    else:
        entity = str(raw).strip()
    if not entity:
        raise ValueError("ac_mode entity must not be empty")
    return entity


def _parse_ac_entities(args: Dict[str, Any]) -> List[str]:
    """
    Parse ac_entities as a non-empty list of entity ID strings.
    """
    raw = args.get("ac_entities")
    if not isinstance(raw, list) or not raw:
        raise ValueError("ac_entities must be a non-empty list")
    entities: List[str] = []
    for idx, item in enumerate(raw):
        entity = str(item).strip()
        if not entity:
            raise ValueError(f"ac_entities[{idx}] must not be empty")
        entities.append(entity)
    return entities


def _parse_settings(raw: Dict[str, Any]) -> Settings:
    """
    Parse and validate the settings block.
    """
    return Settings(
        off_hysteresis=_parse_positive_float(raw, "off_hysteresis"),
        on_hysteresis=_parse_positive_float(raw, "on_hysteresis"),
    )


def _parse_positive_float(source: Dict[str, Any], key: str) -> float:
    """
    Read a required float field and enforce > 0.
    """
    value = source.get(key)
    if value is None:
        raise ValueError(f"settings.{key} is required")
    try:
        value_float = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"settings.{key} must be a number") from exc
    if value_float <= 0:
        raise ValueError(f"settings.{key} must be > 0")
    return value_float
