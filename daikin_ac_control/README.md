# daikin_ac_control

AppDaemon app that works around the Daikin AC system's built-in 4°C cooling hysteresis.

## Problem

Daikin's master AC controller has a fixed internal hysteresis of ~4°C. A setpoint of 22°C causes
the system to keep blowing cold air until the measured temperature drops to ~18°C, significantly
overshooting the target. This app intercepts that behaviour using Home Assistant climate entities
and replaces Daikin's hysteresis with configurable, tighter thresholds.

## How It Works

The app monitors a global AC mode entity. It only acts when that entity reports `0` (cold mode on a KNX binary sensor: `0` = cold, `1` = heat).

For each configured climate entity, the app tracks a per-room management state and reacts to
temperature changes with a two-stage intervention:

```
current - target < -switch_to_ventilation_hysteresis  →  switch to fan-only (ventilation)
current - target < -on_off_ac_hysteresis              →  turn the entity off entirely
current - target >  on_off_ac_hysteresis              →  turn back on in cooling mode
```

### State Machine

Each entity progresses through these states independently:

```
IDLE ──(entity enters cool mode)──▶ COOLING
  ▲                                    │
  │◀──(user manually leaves cool)──────┘
  │                                    │ current - target < -switch_hysteresis
  │                                    ▼
  │                               VENTILATION
  │◀──(user manually off/heat/dry)─────┤
  │                                    │ current - target < -on_off_hysteresis
  │◀──(user manually cool)─────────────│──────────────▶ COOLING
  │                                    ▼
  │                                   OFF
  │◀──(user manually fan_only/heat/dry)┤
  └◀──(user manually cool)─────────────┤
                                       │ current - target > on_off_hysteresis
                                       └──────────────▶ COOLING
```

**IDLE**: Not managing this entity. Watching for it to enter `cool` hvac_mode.

**COOLING**: Entity is in `cool` mode and Daikin is working. App only monitors temperature.

**VENTILATION**: App switched the entity to `fan_only` to slow down overshoot. Daikin's compressor
is off but the fan circulates air.

**OFF**: App turned the entity off. Waiting for the room to warm back up before resuming cooling.

### Shortcut: Skip-to-OFF

If the temperature is already below `target - on_off_ac_hysteresis` when the app first observes
an entity in cooling mode (e.g. after an app restart), it transitions directly to OFF rather than
passing through VENTILATION first.

### Manual Override Handling

The app detects manual changes by comparing the observed `hvac_mode` against the state it expects:

- **COOLING** expects `cool`. Any other mode → retreat to IDLE.
- **VENTILATION** expects `fan_only`. If `cool` is observed → resume COOLING. Any other mode → IDLE.
- **OFF** expects `off`. If `cool` is observed → resume COOLING. Any other mode → IDLE.

Entities in IDLE are picked up again automatically as soon as they re-enter `cool` mode.

### Global Mode Changes

When the mode entity changes away from `0`, the app discards all per-entity state and stops
acting. Entities are left in whatever state they are in at that point. When the mode returns to
`0`, the app initialises fresh from the current observed entity states.

## Configuration

Add the following to your private `apps/apps.yaml`:

```yaml
DaikinAcControl:
  module: daikin_ac_control.daikin_ac_control
  class: DaikinAcControl
  ac_mode: select.climate_mode
  ac_entities:
    - climate.living_room_ac
    - climate.bedroom_ac
    - climate.office_ac
  settings:
    switch_to_ventilation_hysteresis: 0.5
    on_off_ac_hysteresis: 1.0
```

| Parameter | Type | Description |
|---|---|---|
| `ac_mode` | string | KNX binary sensor controlling the global Daikin AC mode. Must read `0` (cold) to activate the app; any other value disables management. |
| `ac_entities` | list of strings | Climate entities to manage (one per room). |
| `settings.switch_to_ventilation_hysteresis` | float (°C) | Degrees below setpoint at which the app switches a cooling entity to fan-only. Must be positive and strictly less than `on_off_ac_hysteresis`. |
| `settings.on_off_ac_hysteresis` | float (°C) | Degrees below setpoint at which the app turns an entity off; also the degrees above setpoint at which it turns back on. |

### Choosing Threshold Values

A typical starting point:

```yaml
switch_to_ventilation_hysteresis: 0.5
on_off_ac_hysteresis: 1.0
```

This means:
- At 0.5°C below target the unit stops compressor cooling but keeps the fan running.
- At 1.0°C below target the unit is turned off entirely.
- The unit turns back on when the room rises 1.0°C above the target.

## Installation

This repository is used as a git submodule under `apps/public_apps` in the private Home Assistant
config repo. AppDaemon resolves the module path as `daikin_ac_control.daikin_ac_control` from
that location. No additional Python dependencies are required beyond AppDaemon itself.

## Running Tests

From the repository root:

```bash
python -m pytest daikin_ac_control/tests/ -v
```

Tests cover all state machine transitions, manual override detection, global mode handling, and
config validation. No AppDaemon installation is required to run them.
