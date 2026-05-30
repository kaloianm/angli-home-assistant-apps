# Gradhermetic Cover Control

Gradhermetic Cover Control wraps an existing Home Assistant blind entity and exposes a user-facing cover that understands [Gradhermetic slat mode](https://gradhermetic.com/en/downloads).

The wrapped blind may be backed by any Home Assistant integration, as long as the underlying entity can report and accept regular blind position commands.

## User-Facing Contract

Each configured blind exposes one virtual cover entity. The virtual cover behaves like a normal Home Assistant cover for regular open, close, stop, and set-position commands.

The application also exposes controls for slat mode:

- `Enter Slat Mode` moves the blind through the mechanical sequence required to engage slat control.
- `Leave Slat Mode` exits slat behavior and returns the virtual cover to regular position control.
- Step up and step down commands use regular blind movement outside slat mode.
- Step up and step down commands use shorter movements while slat mode is active.

Entering slat mode avoids excessive downward movement. If the application is unsure where the blind is, it recovers by moving upward first.

## Position And Restart Behavior

The application durably remembers the last virtual position it commanded for each blind.

After Home Assistant or AppDaemon restarts, the application compares its stored position with the position reported by the underlying blind controller. If they do not match, the application moves upward until the physical controller and stored virtual position are aligned.

This upward-only recovery rule protects the Gradhermetic mechanism from accidental extra downward movement while the blind may already be in or near slat mode.

## YAML Configuration

```yaml
gradhermetic_living_room:
  module: gradhermetic_cover_control
  class: GradhermeticCoverControl

  # Existing Home Assistant cover entity controlled by KNX, Shelly, or another backend.
  real_cover: cover.living_room_blind

  # Identity for the virtual cover and related controls.
  virtual_id: living_room
  virtual_name: "Living Room Blind"

  # Mechanical slat-mode zone and movement tuning.
  tilt_lower_pct: 3.0
  tilt_upper_pct: 10.0
  epsilon_pct: 2.0

  # Total travel time for 0 -> 100%, used when movement must be timed.
  full_travel_time_secs: 60.0

  # Step sizes for regular position movement and slat-mode movement.
  step_pct: 5.0
  slat_step_pct: 1.0
```
