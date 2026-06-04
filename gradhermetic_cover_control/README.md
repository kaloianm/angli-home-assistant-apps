# Gradhermetic Cover Control

Gradhermetic Cover Control wraps an existing Home Assistant blind entity and exposes a user-facing cover that understands [Gradhermetic slat mode](https://gradhermetic.com/en/downloads). The physical Gradhermetic cover is a motor-driven blind with ordinary up and down travel; a specific sequence of movements latches the mechanism in and out of slat mode. Once in slat mode, further up and down movements open and close the slats instead of changing the overall blind position.

The wrapped blind may be backed by any Home Assistant integration, as long as the underlying entity can report and accept regular blind position commands.

## User-Facing Contract

Each configured blind exposes one virtual cover entity. The virtual cover supports the standard Home Assistant cover services: `cover.open_cover`, `cover.close_cover`, `cover.stop_cover`, `cover.toggle`, and `cover.set_cover_position`.

The application also registers a custom Home Assistant service, `gradhermetic_cover_control.set_slat_mode`, which accepts `true` to enter slat mode and `false` to leave it. Entering slat mode moves the blind through the mechanical sequence required to engage slat control; leaving slat mode returns the virtual cover to regular position control.

Outside slat mode, the standard cover services target the full blind travel range: for example, `cover.open_cover` opens the blind to `100%`, and `cover.close_cover` closes it to `0%`. Inside slat mode, the same services target the configured slat-control range instead: for example, `cover.open_cover` moves toward `tilt_upper_pct`, and `cover.close_cover` moves toward `tilt_lower_pct`, so those movements open and close the slats rather than changing the overall blind height.

Entering slat mode avoids excessive downward movement. If the application is unsure where the blind is, it recovers by moving upward to fully-open first.

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
