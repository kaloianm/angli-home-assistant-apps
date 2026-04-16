# ExtractorFanControl

ExtractorFanControl automates bathroom (or other enclosed room) extractor fans
using room light activity plus an optional daily freshness run.

## Overall behavior

- If a light is switched on briefly (below the activation threshold), the fan
  never starts.
- If the light remains on long enough, the fan starts and is kept alive with
  periodic ON pulses so the KNX staircase timer does not expire.
- When the light turns off after a short visit, the fan stops immediately.
- When the light turns off after a longer visit, the fan keeps running for a
  proportional post-run period.
- Long-visit post-run is capped at 10 minutes.
- If a daily run is already active when the light turns off, remaining daily
  schedule time and light-based post-run are merged (fan stays on until the
  later end time).
- An optional daily run can turn the fan on at a fixed time for a fixed
  duration.
- If daily-run demand and occupancy demand overlap, the fan remains on until
  both demands are satisfied.
- Manual fan toggles act as an authoritative temporary override and are cleared
  after a full light OFF -> ON cycle.

## YAML configuration example

```yaml
ExtractorFanControl:
  # AppDaemon module/class entrypoint
  module: extractor_fan_control.extractor_fan_control
  class: ExtractorFanControl

  # KNX staircase actuator auto-off interval in seconds. The settings for the relay controlling the
  # fans (in ETS) need to be:
  #  - Feedback ON
  #  - Time delays OFF
  #  - Staircase function ON
  #    - Staircase time 30 seconds
  #    - Staircase time retriggerable ON
  #    - Switch-on delay OFF
  #    - Reaction to OFF-telegram "switch off"
  #    - At the end of the staircase time "switch off"
  staircase_interval_seconds: 30

  # Keepalive pulse guard in seconds (pulse interval = staircase - guard)
  pulse_guard_seconds: 5

  # One configuration block per light/fan room pair
  pairs:
    - name: first_bathroom
      # Light entity in the same room as the fan
      light_entity: light.first_bathroom_ceiling_light
      # Fan switch entity controlled through KNX staircase function
      fan_switch_entity: switch.first_bathroom_air_extractor
      # Minimum light-on seconds before fan automation activates
      min_light_on_for_fan_seconds: 10
      # Below this light-on duration, fan stops immediately on light-off
      short_visit_threshold_seconds: 60
      # Optional: daily scheduled run start time (24h HH:MM)
      daily_run_time: "07:00"
      # Optional: daily run duration in seconds
      daily_run_duration_seconds: 900

    - name: second_bathroom
      light_entity: light.second_bathroom_ceiling
      fan_switch_entity: switch.second_bathroom_air_extractor
      min_light_on_for_fan_seconds: 15
      short_visit_threshold_seconds: 60
      # No daily run for this room (omit both fields below)
      # daily_run_time: "13:30"
      # daily_run_duration_seconds: 600
```

## Tests

From repository root:

```bash
PYTHONPATH=./apps python3 -m unittest discover -s ./apps/extractor_fan_control/tests -p "test_*.py"
```
