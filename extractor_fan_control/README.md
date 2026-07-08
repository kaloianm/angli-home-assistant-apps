# Extractor Fan Control

Extractor Fan Control runs bathroom extractor fans from room light activity, with an optional daily freshness run.

It is intended for fans wired through staircase timers: turning the fan switch on starts the fan for a fixed actuator-side interval, and repeated on commands keep that interval alive.

Implementation notes, runtime internals, and test instructions live in [IMPLEMENTATION.md](IMPLEMENTATION.md).

## User-Facing Behavior

Each configured light/fan pair is managed independently.

When the room light turns on, the fan does not start immediately. The light must stay on for
`min_light_on_for_fan_seconds` (usually 5 seconds) before the app treats the room as occupied and turns the fan on.

When the light turns off:

- If the light was on for strictly less than `short_visit_threshold_seconds` (usually 1 minute), the fan is turned off immediately.
- If the light was on for at least `short_visit_threshold_seconds`, the fan keeps running for a post-run period based on the light-on duration, capped at 10 minutes.

If a daily freshness run is configured, the fan turns on at `daily_run_time` and remains on until `daily_run_duration_seconds` has elapsed.

If a daily freshness run overlaps with occupancy-based demand, the fan stays on until both demands have ended. In other words, if the freshness run is ongoing, turning off the light won't turn off the fan.

## Manual Fan Control

The app fully owns the fan switch and does not observe manual toggles. You can still operate the switch by hand, but the app does not track it: a manual ON simply runs the fan for one KNX staircase interval, and while the app has active demand its keepalive re-asserts the fan within one keepalive interval. Automation never reacts to a manual toggle, so a hand toggle can never suppress a light-driven run or trigger the fan on its own.

## Exposed Entities

This app does not create new Home Assistant entities.

It uses these existing entities for each configured room:

- `light_entity`: room light used as the occupancy signal.
- `fan_switch_entity`: fan switch controlled by the app.

## Restart Behavior

Runtime state is not persisted across AppDaemon restarts.

After restart, the app registers its listeners and daily schedules again, but it does not reconstruct an in-progress light session, post-run, or keepalive from current Home Assistant entity state. This means that the fan will turn off when the staircase timer expires. The next light or schedule event starts a new control decision.

## YAML Configuration

Add the following to `apps/apps.yaml`:

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
