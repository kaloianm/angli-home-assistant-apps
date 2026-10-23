# Extractor Fan Control

Runs bathroom extractor fans from room light activity, with an optional daily freshness run. Meant
for fans on KNX staircase timers: an ON command runs the fan for a fixed interval, and repeated ONs
keep it running.

Internals, KNX actuator settings and tests: [IMPLEMENTATION.md](IMPLEMENTATION.md).

## Behavior

Each light/fan pair is independent.

- **Light on:** the fan starts once the light has stayed on for `min_light_on_for_fan_seconds`.
- **Light off after less than `short_visit_threshold_seconds`:** the fan stops immediately.
- **Light off after a longer visit:** the fan keeps running for as long as the light was on, up to
  10 minutes.
- **Daily run:** if configured, the fan runs from `daily_run_time` for `daily_run_duration_seconds`.

When demands overlap, the fan runs until the last one ends. Turning the light off during a daily run
doesn't stop the fan.

**Manual control:** the app doesn't watch the fan switch. A manual ON runs the fan for one staircase
interval. A manual OFF while the app wants the fan running is undone by the next keepalive pulse.
Manual runs show no countdown.

## Countdown Sensor

Each pair gets a timestamp sensor, `off_at_sensor_entity`, holding when the fan will turn off. A
dashboard tile shows it as a live countdown. It is `unknown` while the light is on, since the post-run
length isn't known until the light goes off, and whenever the fan isn't running under automation.
When a post-run and a daily run overlap it shows the later end.

## Restarts

Nothing is persisted. After a restart, an in-progress run isn't resumed, and the fan stops when its
staircase timer expires. Countdown sensors are reset to `unknown`. The next light or schedule event
starts fresh.

## Configuration

In `apps/apps.yaml`:

```yaml
ExtractorFanControl:
  module: public_apps.extractor_fan_control.extractor_fan_control
  class: ExtractorFanControl

  staircase_interval_seconds: 30
  pulse_guard_seconds: 5

  pairs:
    - name: first_bathroom
      light_entity: light.first_bathroom_ceiling_light
      fan_switch_entity: switch.first_bathroom_air_extractor
      off_at_sensor_entity: sensor.first_bathroom_air_extractor_off_at
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

| Parameter | Description |
|---|---|
| `staircase_interval_seconds` | The KNX actuator's staircase interval. |
| `pulse_guard_seconds` | Keepalive pulses are sent every `staircase_interval_seconds - pulse_guard_seconds`. Must be smaller than the interval. |
| `pairs[].name` | Unique name for logs. Optional; generated if omitted. |
| `pairs[].light_entity` | Light used as the occupancy signal. |
| `pairs[].fan_switch_entity` | Fan switch to control. |
| `pairs[].off_at_sensor_entity` | Optional countdown sensor id, must start with `sensor.`. Default `sensor.extractor_fan_<name>_off_at`. |
| `pairs[].min_light_on_for_fan_seconds` | Light-on time before the fan starts. `>= 0`, `<=` the short-visit threshold. |
| `pairs[].short_visit_threshold_seconds` | Visits shorter than this stop the fan immediately at light-off. `> 0`. |
| `pairs[].daily_run_time` | Optional `HH:MM` start of the daily run. Needs the duration too. |
| `pairs[].daily_run_duration_seconds` | Optional daily run length. Needs the time too. |

All durations are integer seconds.
