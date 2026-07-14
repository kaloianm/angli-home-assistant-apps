# Gradhermetic Cover Control

Gradhermetic Cover Control wraps an existing Home Assistant blind entity and exposes a user-facing cover that understands Gradhermetic **tilt mode** (slat-orientation control). See the [Gradhermetic downloads](https://gradhermetic.com/en/downloads) for the physical product. The physical Gradhermetic cover is a motor-driven blind with ordinary up and down travel; a specific sequence of movements latches the mechanism in and out of tilt mode. Once in tilt mode, further up and down movements change the slat orientation instead of the overall blind position.

The wrapped blind may be backed by any Home Assistant integration, as long as the underlying entity can report and accept regular blind position commands.

Implementation notes, runtime internals, and test instructions live in [IMPLEMENTATION.md](IMPLEMENTATION.md).

## User-Facing Contract

Each configured blind exposes one virtual cover entity, surfaced to Home Assistant as a template cover that forwards commands to the application. The virtual cover supports the standard Home Assistant cover services: `cover.open_cover`, `cover.close_cover`, `cover.stop_cover`, `cover.toggle`, and `cover.set_cover_position`.

Tilt mode is toggled by firing a `gradhermetic_command` event with `command: set_tilt_mode` and `enabled: true|false` — `true` enters tilt mode, `false` leaves it. This is the Home-Assistant-facing entry point (usable from any script, button, or automation) because the template cover already speaks to the app over that event bus. Entering tilt mode moves the blind through the mechanical sequence required to engage slat control; leaving tilt mode returns the virtual cover to regular position control.

> The app additionally calls AppDaemon's `register_service` for `gradhermetic_cover_control/set_tilt_mode`, but that registers a service in AppDaemon's own namespace, not a Home Assistant service callable from HA scripts or the UI. Prefer the event form above from Home Assistant.

For step and tilt from the UI, the app watches three `input_button` helpers per blind. `..._tilt` toggles tilt mode (enter/leave). `..._step_up` and `..._step_down` adjust **slats only**: while latched they step the slat angle by `tilt_step_pct` and clamp at both zone edges, and while not latched they do nothing. Unlike a KNX wall button (below), the step helpers never enter or leave tilt and never move the whole blind — entering and leaving tilt is the tilt helper's job. This split exists because the UI has a dedicated tilt control, whereas a two-button wall switch does not and must reach tilt through its step presses.

The guiding principle for every control surface is **up = more light, down = less light** — applied to the whole blind's height when outside the tilt zone, and to the slat angle when inside it.

### Outside tilt mode

The standard cover services target the full blind travel range:

- `cover.open_cover` opens the blind fully (`100%`).
- `cover.close_cover` closes the blind fully (`0%`).
- `cover.set_cover_position` moves to the requested absolute position.

### Inside tilt mode

Once latched, the same services control slat orientation within the narrow **tilt zone** between `tilt_zone_lower_pct` and `tilt_zone_upper_pct`:

- `cover.open_cover` orients the slats perpendicular to the window (most light) — the blind sits at `tilt_zone_lower_pct`.
- `cover.close_cover` orients the slats parallel to the window (least light) — the blind sits at `tilt_zone_upper_pct`.
- `cover.set_cover_position` interpolates between these two ends: `100%` maps to `tilt_zone_lower_pct` (slats fully open / perpendicular) and `0%` maps to `tilt_zone_upper_pct` (slats fully closed / parallel).

Note the inversion relative to normal travel: inside the tilt zone a *higher* absolute blind position means *more closed* slats.

## Entering And Leaving Tilt Mode

The latch sequence assumes the blind starts **outside** the tilt zone. This is important: the mechanism only latches when the full down-then-up motion is performed across the lower edge of the zone.

To enter tilt mode the app performs a genuine down-then-up motion across the lower edge:

1. Ensure the blind is safely above the lower edge so the dip crosses it downward. If the start position lies inside the ambiguity band (`[lower - epsilon, upper + epsilon]`) the latch might already be engaged — for example an earlier entry that was interrupted mid-rise physically latches the moment the rise crosses the lower edge — so the sequence first rises above the zone (`tilt_zone_upper_pct + tilt_zone_epsilon_pct`) to release the latch before dipping, since the latch only releases upward. If the blind instead starts clearly below the band it first hops up to `tilt_zone_lower_pct + tilt_zone_epsilon_pct`; if its position is unknown it recovers fully open first.
2. Move down to `tilt_zone_lower_pct - tilt_zone_epsilon_pct` (dip below the lower edge).
3. Move up to `tilt_zone_upper_pct`. The upward crossing of the lower edge latches the mechanism in tilt mode, with the slats parallel (closed).

To leave tilt mode:

- Move up to above `tilt_zone_upper_pct` (specifically `tilt_zone_upper_pct + tilt_zone_epsilon_pct`). Leaving is always an upward move; the application never drives downward to disengage.

`tilt_zone_epsilon_pct` is the clearance margin used to cleanly cross the lower edge when engaging and the upper edge when disengaging.

Entering tilt mode avoids excessive downward movement. If the application is unsure where the blind is, it first recovers by moving fully open (see "Position And Restart Behavior") before running the latch sequence.

## Wall-Button (KNX) Control

The application can be driven by a two-button KNX wall switch (an up button and a down button, each distinguishing a short press from a long press). This maps onto the two standard KNX blind communication objects:

- A **"Move"** group address that receives **long** presses.
- A **"Stop/Step"** group address that receives **short** presses.

In both cases the telegram's value selects the direction (up = more light, down = less light). These group addresses are surfaced to the application as `knx_event`s on the Home Assistant event bus.

### Long press — jump to an extreme

- **Long up** drives the blind fully open (`100%`). If it is currently in tilt mode, this naturally leaves tilt mode (the exit is upward anyway).
- **Long down** drives the blind fully closed (`0%`). If the latch might be engaged (it is in tilt mode, or its position is inside/near the tilt zone), the blind first rises out of the zone to release the latch — which only releases upward — and then descends.

### Short press — stop, or step in the more-light / less-light direction

A short press is evaluated in this priority order:

1. **If the blind is currently moving, stop it.** (This matches the native KNX "Stop/Step" behavior.)
2. **Otherwise, if inside the tilt zone, step the slats** by `tilt_step_pct` — up steps toward open (more light), down steps toward closed (less light).
3. **Otherwise (idle, outside the tilt zone), enter the tilt zone** when the press points *toward* it:
   - From above the zone, a **down** press enters tilt mode at the most-closed end (its near edge).
   - From below the zone, an **up** press enters tilt mode at the most-open end (its near edge).
   - A short press pointing *away* from the zone (when already past it) does nothing — long press covers the extremes.

Stepping naturally crosses the zone boundaries: an up step at the open edge of the zone leaves tilt mode upward and resumes whole-blind control, and a down press from just above the zone enters it. Boundary crossings execute the full engage/disengage sequence rather than a small `tilt_step_pct` nudge.

## Position And Restart Behavior

The application does not persist state across restarts. After Home Assistant or AppDaemon restarts, it re-establishes a known-safe state from the position reported by the underlying blind controller:

- If the reported position is clearly **outside** the tilt zone, the blind cannot be latched, so the application resumes whole-blind control from that position.
- If the reported position is **inside or near** the tilt zone, the latch state is ambiguous, so the application issues a full `cover.open_cover` (it sends the open command rather than a concrete target position) to drive fully open, then treats itself as being at `100%` with tilt mode off.

This upward-only recovery rule protects the Gradhermetic mechanism from accidental extra downward movement while the blind may already be in or near the tilt zone.

The same protection applies during normal operation, not just at restart: whenever the latch might be engaged — the blind is believed in tilt mode, its reported position lies inside the `[lower - epsilon, upper + epsilon]` band, or its position is unknown — any command that would drive the blind downward first rises above the zone to release the latch, then descends. This keeps the mechanism safe even if the app's belief about the latch state was disturbed by an interrupted tilt sequence or by an external command sent directly to the underlying cover.

## YAML Configuration

```yaml
gradhermetic_living_room:
  module: public_apps.gradhermetic_cover_control.gradhermetic_cover_control
  class: GradhermeticCoverControl

  # Existing Home Assistant cover entity controlled by KNX, Shelly, or another backend.
  real_cover: cover.living_room_blind

  # Identity for the virtual cover and related controls.
  virtual_id: living_room
  virtual_name: "Living Room Blind"

  # Mechanical tilt-zone bounds and movement tuning.
  tilt_zone_upper_pct: 44.0
  tilt_zone_lower_pct: 38.0
  tilt_zone_epsilon_pct: 2.0

  # Slat step size (virtual %) for short presses while inside the tilt zone. Because the actuator
  # reports integer positions, a step must map to at least one whole real percent, i.e.
  # tilt_step_pct >= 100 / (tilt_zone_upper_pct - tilt_zone_lower_pct). For a 6% zone that is ~16.7,
  # so a 6% zone yields roughly six usable slat positions. The app rejects a smaller step at startup.
  tilt_step_pct: 20.0

  # Optional KNX wall-button group addresses. The "move" address receives long
  # presses; the "step" address receives short presses. Direction (up/down) is
  # carried by the telegram value.
  knx_move_address: "1/2/3"
  knx_step_address: "1/2/4"
```
