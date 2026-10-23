# Gradhermetic Cover Control

Wraps an existing Home Assistant blind entity in a virtual cover that understands Gradhermetic
**tilt mode** ([product](https://gradhermetic.com/en/downloads)). The blind is an ordinary up/down
motor, but a specific down-then-up sequence latches the mechanism so that further movement rotates
the slats instead of moving the blind. The real cover can come from any integration that reports and
accepts positions.

Internals, Home Assistant wiring and tests: [IMPLEMENTATION.md](IMPLEMENTATION.md).

## Controls

Throughout: **up = more light, down = less light**. Outside tilt mode that applies to the blind's
height; in tilt mode, to the slat angle.

### Cover services

The virtual cover supports `open_cover`, `close_cover`, `stop_cover`, `toggle` and
`set_cover_position`.

| | Outside tilt mode | In tilt mode |
|---|---|---|
| `open_cover` | Fully open (100%) | Slats perpendicular, most light (blind at `tilt_zone_lower_pct`) |
| `close_cover` | Fully closed (0%) | Slats parallel, least light (blind at `tilt_zone_upper_pct`) |
| `set_cover_position` | Absolute height, snapped out of the ambiguity band (below) | Slat angle: 100% = `tilt_zone_lower_pct`, 0% = `tilt_zone_upper_pct` |

In tilt mode the slider is inverted relative to the blind: a higher real position means more closed
slats. This virtual slat scale exists only on the slider. Every number in the configuration is real
blind travel.

**The ambiguity band** runs from `tilt_zone_lower_pct - tilt_zone_epsilon_pct` to
`tilt_zone_release_pct`. A latched mechanism can rest anywhere in it, and rising into it can latch
silently, so height moves never stop inside it. A target inside snaps to the nearer edge, or to the
far edge if the blind already rests on the nearer one.

### Tilt mode

Fire a `gradhermetic_command` event with `virtual_id`, `command: set_tilt_mode` and
`enabled: true|false`. Entering a mode the blind is already in, or already entering, does nothing.
Asking for the opposite of a sequence in progress stops it where it is.

### Dashboard buttons

The app listens to three `input_button` helpers per blind:

- `..._tilt` toggles tilt mode, or cancels an entry or exit in progress.
- `..._step_up` / `..._step_down`:
  1. If anything is moving, stop it.
  2. Otherwise, in tilt mode, step the slats by `tilt_step_pct`, stopping at the zone edges.
  3. Otherwise, step the height by `height_step_pct`. A step into the ambiguity band continues to
     the band edge ahead of it.

A step never enters tilt mode. A step with nowhere to go is logged and ignored.

### KNX wall buttons

A two-button KNX switch (short and long press per button) can drive the app through three group
addresses. **Nothing else may listen on them**, in particular not the actuator: a button wired
directly to the actuator bypasses the app.

- `knx_move_address` (long press): up opens fully, leaving tilt mode if it is engaged. Down closes
  fully.
- `knx_step_address` (short press): the dashboard step rule, except that an up step at the open slat
  edge leaves tilt mode, since a two-button switch has no other way out.
- `knx_tilt_address`: toggles tilt mode, like the `..._tilt` button. It acts on `1` and ignores
  `0`, so configure the button to send ON only.

Telegram value `0` = up, `1` = down.

### Published entities

- `sensor.gradhermetic_<id>_position`: the virtual position, with a `motion` attribute (`opening` /
  `closing` / `idle`). Updated during travel. `unavailable` when the real position is unreadable.
- `binary_sensor.gradhermetic_<id>_tilt_mode`: `on` while in tilt mode, which is how you know whether
  the position above is a slat angle or a height. A tilt toggle on a dashboard should read this for
  its label, since the `..._tilt` button has no state.

During an entry the cover stays in height mode until the mechanism actually latches. An exit switches
to height mode immediately.

## Entering and Leaving Tilt Mode

Reported percentages are only accurate after the actuator has run against its top limit switch, and
the tilt zone is only a few percent wide. So entry always starts from the top:

1. Open fully with `open_cover`, which re-references the actuator. Skipped if already at 100%.
2. Move down to `tilt_zone_lower_pct - tilt_zone_epsilon_pct`, just below the zone.
3. Move up to `tilt_zone_upper_pct`. Crossing the lower edge upward latches, with the slats closed.
4. Move to `tilt_zone_enter_pct` if it is set and differs from the upper edge.

Leaving tilt mode opens the blind fully. It counts as released once the blind passes
`tilt_zone_release_pct`.

**The mechanism is never driven down while it might be latched.** Any descent opens fully first
unless the latch is known to be released. Closing right after leaving tilt mode goes straight down.

## Restarts and the Latch Belief

Nothing is persisted. The app tracks the latch as **latched**, **released** or **unknown**:

- Only a completed entry sets **latched**.
- Resting outside the ambiguity band sets **released**.
- It becomes **unknown** after an interrupted sequence, the cover going unavailable, unexpected
  motion ending in the band, or a position-commanded rise ending exactly on `tilt_zone_release_pct`
  (zero margin, so it can't be trusted).

After a restart the belief is seeded from the reported position, and **the blind does not move**. If
that position is inside the band, the belief is unknown, and the next descent opens fully first.
Step up still works directly from there.

**Known gap:** moving the real cover directly (its own entity, the device's app, or KNX objects wired
to the actuator) can leave the blind latched but resting outside the band, which the app will then
read as released. Keep direct controls of the real cover off everyday dashboards.

## Configuration

All percentages are whole numbers of real blind travel. Fractions such as `38.5` are rejected at
startup.

```yaml
gradhermetic_living_room:
  module: public_apps.gradhermetic_cover_control.gradhermetic_cover_control
  class: GradhermeticCoverControl

  real_cover: cover.living_room_blind
  virtual_id: living_room
  virtual_name: "Living Room Blind"

  # Tilt zone edges: upper = slats closed, lower = slats fully open.
  tilt_zone_upper_pct: 44
  tilt_zone_lower_pct: 38

  # How far below the lower edge the entry dips. >= 1.
  tilt_zone_epsilon_pct: 2

  # Required. Measured height at which the latch releases; > upper, < 100. Also the top of the
  # ambiguity band.
  tilt_zone_release_pct: 50

  # Optional. Where entry finishes, inside [lower, upper]. Defaults to upper (closed).
  tilt_zone_enter_pct: 42

  # Slat step, 1 .. (upper - lower).
  tilt_step_pct: 2

  # Optional. Height step outside tilt mode, 1 .. 100. Defaults to 2.
  height_step_pct: 2

  # Optional KNX wall-button group addresses.
  knx_move_address: "1/2/3"
  knx_step_address: "1/2/4"
  knx_tilt_address: "1/2/5"
```

The band (`lower - epsilon` to `release`) must stay strictly inside 0-100, so a blind resting on an
end stop always counts as released.

### Calibrating `tilt_zone_release_pct`

Latch the blind, then raise it a percent or two at a time. While still latched, only the slats turn.
The height at which the whole blind starts to lift is the release height. Round up.

Err high. Too low is the one failure the design can't absorb: the app believes the latch is free and
the next descent goes straight down on a latched blind. Too high only widens the band that
`set_cover_position` skips.

### Calibrating `tilt_zone_lower_pct`

Set it to the height at which the slats are fully open. Slider 100% targets this edge exactly, with
no safety margin. A margin would only guard against sub-percent overshoot, and the app never aims a
descent below this edge while latched.

### Calibrating `tilt_zone_enter_pct`

The latching rise ends with the slats closed, and on many blinds they don't visibly open until a
couple of percent below the upper edge, so an entry there looks like it did nothing. Set this to
where the slats are as open as you want tilt mode to start. On a `[29, 34]` zone, `32` is slightly
open.
