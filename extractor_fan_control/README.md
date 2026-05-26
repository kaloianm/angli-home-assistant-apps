# extractor_fan_control

AppDaemon app that controls KNX staircase-timer extractor fans from room light activity, with an
optional daily freshness run.

## Problem

Bathroom extractor fans are wired through KNX staircase timers: an ON command starts the fan for a
fixed actuator-side interval, and repeated ON pulses keep that interval alive. The desired automation
is more nuanced than a simple light follower:

- brief light usage should not start the fan;
- real occupancy should start the fan after a threshold;
- long visits should get a proportional post-run;
- daily freshness runs should merge cleanly with occupancy demand;
- manual fan toggles should temporarily override automation without causing command echo loops.

## How It Works

Each configured light/fan pair is managed independently. The pure logic state machine receives light
events, fan switch events, daily schedule events, and timer ticks. It returns declarative actions for
the AppDaemon adapter to execute.

```
light on
  -> wait min_light_on_for_fan_seconds
  -> fan on + keepalive pulses

light off after short visit
  -> fan off

light off after long visit
  -> post-run for min(light-on duration, max_post_run_seconds)

daily schedule
  -> fan on until daily_run_duration_seconds has elapsed
```

When occupancy and daily schedule demand overlap, the fan stays on until both demand windows have
expired.

### State Machine

Each pair exposes these internal states in logs:

```
IDLE --(light on)--> WAITING_FOR_ACTIVATION --(threshold reached)--> RUNNING_LIGHT
  ^                         |                                      |
  |                         +--(light off before threshold)--------+
  |                                                                |
  +--(short visit light off)---------------------------------------+
                                                                   |
                                                                   +--(long visit light off)--> POST_RUN --(deadline)--> IDLE
                                                                   |
daily schedule --> SCHEDULED_RUN --(deadline)--> IDLE              |
        |                                                          |
        +---------- overlap with light/post-run ----------> COMBINED_RUN

any state --(manual fan switch)--> MANUAL_OVERRIDE --(full light OFF -> ON cycle)--> current demand state
```

**IDLE**: No light, occupancy, schedule, or manual demand is active.

**WAITING_FOR_ACTIVATION**: Light is on, but has not yet stayed on long enough to start the fan.

**RUNNING_LIGHT**: Light-based occupancy demand is active while the light remains on.

**POST_RUN**: Light has turned off after a long visit; fan remains on until the computed post-run
deadline.

**SCHEDULED_RUN**: Daily freshness run is active without light-based demand.

**COMBINED_RUN**: Schedule and occupancy/post-run demand overlap; the later deadline wins.

**MANUAL_OVERRIDE**: A user changed the fan switch. Automation follows that manual fan state and does
not command the same state back into KNX. The override clears after a complete light OFF -> ON cycle.

## KNX Keepalive

The fan switch is expected to be backed by a KNX staircase function. While automation or manual ON
override requires the fan to run, the app sends periodic ON pulses at:

```
staircase_interval_seconds - pulse_guard_seconds
```

For a 30 second staircase timer and 5 second guard, the app sends an ON pulse every 25 seconds.

## Configuration

Add the following to your private `apps/apps.yaml`:

```yaml
ExtractorFanControl:
  module: extractor_fan_control.extractor_fan_control
  class: ExtractorFanControl

  staircase_interval_seconds: 30
  pulse_guard_seconds: 5

  pairs:
    - name: first_bathroom
      light_entity: light.first_bathroom_ceiling_light
      fan_switch_entity: switch.first_bathroom_air_extractor
      min_light_on_for_fan_seconds: 10
      short_visit_threshold_seconds: 60
      daily_run_time: "07:00"
      daily_run_duration_seconds: 900

    - name: second_bathroom
      light_entity: light.second_bathroom_ceiling
      fan_switch_entity: switch.second_bathroom_air_extractor
      min_light_on_for_fan_seconds: 15
      short_visit_threshold_seconds: 60
```

| Parameter | Type | Description |
|---|---|---|
| `staircase_interval_seconds` | integer seconds | KNX actuator staircase auto-off interval. |
| `pulse_guard_seconds` | integer seconds | Safety margin subtracted from the staircase interval for keepalive pulses. |
| `pairs[].name` | string | Unique pair name used in logs and timer callbacks. Optional; generated if omitted. |
| `pairs[].light_entity` | string | Light entity used as occupancy input. |
| `pairs[].fan_switch_entity` | string | KNX-backed fan switch entity to control. |
| `pairs[].min_light_on_for_fan_seconds` | integer seconds | Continuous light-on duration required before fan starts. |
| `pairs[].short_visit_threshold_seconds` | integer seconds | Light-on duration below which fan stops immediately when the light turns off. |
| `pairs[].daily_run_time` | `HH:MM` string | Optional daily freshness run start time. Omit with duration to disable. |
| `pairs[].daily_run_duration_seconds` | integer seconds | Optional daily freshness run duration. Omit with time to disable. |

### KNX Actuator Settings

The relay controlling the fan should be configured roughly as:

- Feedback ON
- Time delays OFF
- Staircase function ON
- Staircase time matching `staircase_interval_seconds`
- Staircase time retriggerable ON
- Switch-on delay OFF
- Reaction to OFF telegram: switch off
- End of staircase time: switch off

## Installation

This repository is used as a git submodule under `apps/public_apps` in the private Home Assistant
config repo. AppDaemon resolves the module path as
`extractor_fan_control.extractor_fan_control` from that location.

## Running Tests

From the repository root:

```bash
python -m pytest extractor_fan_control/tests/ -v
```

Tests cover light-based state transitions, post-run deadlines, daily schedule overlap, manual
override behavior, config validation, and runtime fan command feedback handling. No AppDaemon
installation is required to run them.
