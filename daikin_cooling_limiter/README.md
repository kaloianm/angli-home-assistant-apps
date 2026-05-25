# daikin_cooling_limiter

AppDaemon app that works around the Daikin AC system's built-in 4°C cooling hysteresis.

## Problem

Daikin's master AC controller has a fixed internal hysteresis of ~4°C. A setpoint of 22°C causes
the system to keep blowing cold air until the measured temperature drops to ~18°C, significantly
overshooting the target. This app intercepts that behaviour using Home Assistant climate entities
and replaces Daikin's hysteresis with configurable, tighter thresholds.

## How It Works

The app monitors a global AC mode entity. It only acts when that entity reports cold mode (`0`,
`off`, or `Cooling`; for KNX boolean variables this is typically `0`/`off` = cold and `1`/`on` = heat).

For each configured climate entity, the app tracks a per-room management state and reacts to
temperature changes:

```
current - target < -off_hysteresis  →  turn the entity off
current - target >  on_hysteresis     →  turn back on in cooling mode
```

### State Machine

Each entity progresses through these states independently:

```
NOT_MANAGED ──(entity enters cool mode)──▶ COOLING
    ▲                                    │
    │◀──(manual fan_only/off)────────────┤
    │                                    │ current - target < -off_hysteresis
    │                                    ▼
    │                          LOWER_TEMP_REACHED
    │◀──(manual fan_only/off)────────────┤
    └◀──(entity enters cool mode)────────┤
                                       │ current - target > on_hysteresis
                                       └──────────────▶ COOLING
```

**NOT_MANAGED**: Not managing this entity. Entered when the user manually sets `fan_only` or `off`, or
when the entity is in any other mode. The app resumes when the entity re-enters `cool` mode.

**COOLING**: Entity is in `cool` mode. App monitors temperature and turns the unit off if the room
drops too far below the setpoint.

**LOWER_TEMP_REACHED**: App turned the entity off after overshoot. Waiting for the room to warm
before resuming cooling.

### Manual Disable

Manual `fan_only` or `off` disables management for that entity until it re-enters `cool` mode.
The app never commands `fan_only`; it only switches between `cool` and off.

### Global Mode Changes

When the mode entity changes away from `0`, the app discards all per-entity state and stops
acting. Entities are left in whatever state they are in at that point. When the mode returns to
`0`, the app initialises fresh from the current observed entity states.

## Configuration

Add the following to your private `apps/apps.yaml`:

```yaml
DaikinCoolingLimiter:
  module: daikin_cooling_limiter.daikin_cooling_limiter
  class: DaikinCoolingLimiter
  ac_mode: select.climate_mode
  ac_entities:
    - climate.living_room_ac
    - climate.bedroom_ac
    - climate.office_ac
  settings:
    off_hysteresis: 0.7
    on_hysteresis: 0.3
```

| Parameter | Type | Description |
|---|---|---|
| `ac_mode` | string | KNX entity controlling the global Daikin AC mode. Must read `0`, `off`, or `Cooling` to activate the app; any other value disables management. |
| `ac_entities` | list of strings | Climate entities to manage (one per room). |
| `settings.off_hysteresis` | float (°C) | Degrees below setpoint at which the app turns a cooling entity off. |
| `settings.on_hysteresis` | float (°C) | Degrees above setpoint at which the app turns an entity back on in cooling mode. |

### Choosing Threshold Values

A typical starting point:

```yaml
off_hysteresis: 0.7
on_hysteresis: 0.3
```

This means:
- At 0.7°C below target the unit is turned off entirely.
- The unit turns back on when the room rises 0.3°C above the target.

## Installation

This repository is used as a git submodule under `apps/public_apps` in the private Home Assistant
config repo. AppDaemon resolves the module path as `daikin_cooling_limiter.daikin_cooling_limiter` from
that location. No additional Python dependencies are required beyond AppDaemon itself.

## Running Tests

From the repository root:

```bash
python -m pytest daikin_cooling_limiter/tests/ -v
```

Tests cover all state machine transitions, manual disable handling, global mode handling, and
config validation. No AppDaemon installation is required to run them.
