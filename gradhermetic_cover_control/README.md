# Gradhermetic Cover Control

Gradhermetic Cover Control wraps an existing Home Assistant blind entity and exposes a user-facing cover that understands Gradhermetic **tilt mode** (slat-orientation control). See the [Gradhermetic downloads](https://gradhermetic.com/en/downloads) for the physical product. The physical Gradhermetic cover is a motor-driven blind with ordinary up and down travel; a specific sequence of movements latches the mechanism in and out of tilt mode. Once in tilt mode, further up and down movements change the slat orientation instead of the overall blind position.

The wrapped blind may be backed by any Home Assistant integration, as long as the underlying entity can report and accept regular blind position commands.

Implementation notes, runtime internals, and test instructions live in [IMPLEMENTATION.md](IMPLEMENTATION.md).

## User-Facing Contract

Each configured blind exposes one virtual cover entity, surfaced to Home Assistant as a template cover that forwards commands to the application. The virtual cover supports the standard Home Assistant cover services: `cover.open_cover`, `cover.close_cover`, `cover.stop_cover`, `cover.toggle`, and `cover.set_cover_position`.

Tilt mode is toggled by firing a `gradhermetic_command` event with `command: set_tilt_mode` and `enabled: true|false` — `true` enters tilt mode, `false` leaves it. This is the Home-Assistant-facing entry point (usable from any script, button, or automation) because the template cover already speaks to the app over that event bus. Entering tilt mode moves the blind through the mechanical sequence required to engage slat control; leaving tilt mode returns the virtual cover to regular position control.

> The app additionally calls AppDaemon's `register_service` for `gradhermetic_cover_control/set_tilt_mode`, but that registers a service in AppDaemon's own namespace, not a Home Assistant service callable from HA scripts or the UI. Prefer the event form above from Home Assistant.

For step and tilt from the UI, the app watches three `input_button` helpers per blind. `..._tilt` toggles tilt mode (enter/leave). `..._step_up` and `..._step_down` do one of two things, depending on whether the mechanism is latched:

- **Latched** — step the slat angle by `tilt_step_pct`, clamping at both zone edges. Unlike a KNX wall button (below), they never step *out* of tilt at the open edge; leaving is the tilt helper's job.
- **Not latched** — enter the tilt zone when the press points toward it, exactly as a wall-button short press would: from above the zone a **down** press enters at the most-closed end, and from below an **up** press enters at the most-open end. A press pointing away from the zone, or one made while the blind rests inside the ambiguity band without a latch belief, does nothing.

The second case exists for blinds with no wall switch: without it, the step buttons would be inert whenever the blind is not already in tilt, leaving the dashboard with no directional way in. A press is ignored outright while a movement is already in progress, so it can never abort a sequence.

The guiding principle for every control surface is **up = more light, down = less light** — applied to the whole blind's height when outside the tilt zone, and to the slat angle when inside it.

### Outside tilt mode

The standard cover services target the full blind travel range:

- `cover.open_cover` opens the blind fully (`100%`).
- `cover.close_cover` closes the blind fully (`0%`).
- `cover.set_cover_position` moves to the requested absolute position, except that a target landing *inside* the tilt zone's ambiguity band (between `tilt_zone_lower_pct - tilt_zone_epsilon_pct` and `tilt_zone_release_pct`) is snapped outward to the nearer edge of that band. Rising into the band would silently engage the latch while the application believed it was still doing height control, so whole-blind moves stay clear of it. The reported position is the snapped value.

  The band's upper end is the height at which the latch genuinely releases, because a latched-but-not-yet-released mechanism can be resting anywhere below it. Configuring a `tilt_zone_release_pct` well above the zone therefore widens the range of heights the blind refuses to stop at — with the default (`tilt_zone_upper_pct + tilt_zone_epsilon_pct`) the adjustment is a couple of percent of travel, but a blind that only releases much higher up will skip past more than that.

### Inside tilt mode

Once latched, the same services control slat orientation within the narrow **tilt zone** between `tilt_zone_lower_pct` and `tilt_zone_upper_pct`:

- `cover.open_cover` orients the slats perpendicular to the window (most light) — the blind sits at `tilt_zone_lower_pct`.
- `cover.close_cover` orients the slats parallel to the window (least light) — the blind sits at `tilt_zone_upper_pct`.
- `cover.set_cover_position` interpolates between these two ends: `100%` maps to `tilt_zone_lower_pct` (slats fully open / perpendicular) and `0%` maps to `tilt_zone_upper_pct` (slats fully closed / parallel).

Note the inversion relative to normal travel: inside the tilt zone a *higher* absolute blind position means *more closed* slats.

## Entering And Leaving Tilt Mode

The mechanism only latches when a full down-then-up motion is performed across the lower edge of the zone. A second hardware fact shapes the sequence just as much: **the percentages are only reliable when the sequence starts from the fully open position.** The actuator's reported position cannot be trusted to match the blind's true physical position unless the move is referenced from the top limit, and the tilt zone is only a few percent wide — so a dip aimed from an unreferenced height may not clear the lower edge at all.

Entering tilt mode is therefore a single sequence, run from wherever the blind happens to be:

1. Drive fully open with `cover.open_cover`. Sending the command rather than a target position makes the actuator run against its own limit switch, which re-references it. This step is skipped only when the blind already reports being fully open.
2. Move down to `tilt_zone_lower_pct - tilt_zone_epsilon_pct` (dip below the lower edge). Starting from fully open this is a pure descent, so it cannot engage the latch on the way down.
3. Move up to `tilt_zone_upper_pct`. The upward crossing of the lower edge latches the mechanism in tilt mode, with the slats parallel (closed).
4. Move to the slat angle given by `tilt_enter_landing_pct` (a virtual slat percentage: `0` is closed, `100` is fully open). This is one more small in-zone move and is omitted when the landing rounds to the position step 3 already reached. It defaults to `0`, i.e. no fourth step at all.

Step 4 exists because the latching rise necessarily ends with the slats fully closed, and on a real blind the slats often do not visibly open until a couple of percent below `tilt_zone_upper_pct` — so an entry that lands exactly on the closed edge looks like it did nothing. Set `tilt_enter_landing_pct` to the slightly-open angle you actually want tilt mode to start at. This applies to deliberate entry (the tilt helper, the event, the service); a wall-button or step-button entry keeps the directional rule below instead, landing on whichever end of the zone the press came toward.

To leave tilt mode:

- Move up to `tilt_zone_release_pct` — the height at which the mechanism genuinely lets go, which defaults to `tilt_zone_upper_pct + tilt_zone_epsilon_pct`. Leaving is always an upward move; the application never drives downward to disengage. This short exit is trusted because the mechanism is only ever *known* to be latched immediately after an entry sequence re-referenced the actuator, with nothing but small in-zone slat moves since.

  The rise is *commanded* two percent higher than the height it has to reach, while still being considered done as soon as the blind reports `tilt_zone_release_pct` or above. Position feedback is only good to a percent or so, so commanding exactly the release height lets a blind that stops slightly low report a successful exit it never physically performed; commanding past it makes that shortfall the application's slack instead of its risk.

Whenever the latch state is instead **uncertain** — after an interrupted sequence, a restart, or a move the application did not command — a release cannot rely on a reported percentage either, so it is a full `cover.open_cover` as well (see "Position And Restart Behavior").

`tilt_zone_epsilon_pct` is the clearance margin used to cleanly cross the lower edge when engaging and the upper edge when disengaging. It must be at least one whole percent, so the rounded command the actuator receives is distinct from the edge it has to clear.

### Calibrating `tilt_zone_release_pct`

`tilt_zone_epsilon_pct` only has to carry the *reported* position clear of the upper edge; the latch itself may need considerably more real travel before it disengages. To measure the difference: latch the blind into tilt mode, then raise it in small increments (a percent or two at a time) and watch it. While the mechanism is still latched the movement only changes the slat angle; the height at which the blind starts lifting *as a whole* is the release height. Set `tilt_zone_release_pct` to that value (rounded up).

Until you have measured it, set it conservatively a few percent above the zone rather than leaving it at the default. An exit that does not physically release is the one failure the design cannot absorb: the application commits to believing the mechanism is free, so the next downward command descends straight away — on a blind that is still latched. Setting the value too *high* costs nothing but a little extra travel on the way out and a slightly wider band of heights that `cover.set_cover_position` refuses to stop at.

Entering tilt mode costs an upward trip to fully open first. That is deliberate: rising is the one direction that is always safe, and the top limit is the only position the actuator cannot be wrong about.

## Wall-Button (KNX) Control

The application can be driven by a two-button KNX wall switch (an up button and a down button, each distinguishing a short press from a long press). This maps onto the two standard KNX blind communication objects:

- A **"Move"** group address that receives **long** presses.
- A **"Stop/Step"** group address that receives **short** presses.

In both cases the telegram's value selects the direction (up = more light, down = less light). These group addresses are surfaced to the application as `knx_event`s on the Home Assistant event bus.

### Long press — jump to an extreme

- **Long up** drives the blind fully open (`100%`). If it is currently in tilt mode, this naturally leaves tilt mode (the exit is upward anyway).
- **Long down** drives the blind fully closed (`0%`). Unless the latch is known to be released, the blind first drives fully open to release it — the latch only releases upward — and then descends.

### Short press — stop, or step in the more-light / less-light direction

A short press is evaluated in this priority order:

1. **If the blind is currently moving, stop it.** (This matches the native KNX "Stop/Step" behavior.)
2. **Otherwise, if the mechanism is latched, step the slats** by `tilt_step_pct` — up steps toward open (more light), down steps toward closed (less light).
3. **Otherwise (idle, not latched), enter the tilt zone** when the press points *toward* it:
   - From above the zone, a **down** press enters tilt mode at the most-closed end (its near edge).
   - From below the zone, an **up** press enters tilt mode at the most-open end (its near edge).
   - A short press pointing *away* from the zone (when already past it) does nothing — long press covers the extremes.
   - A short press while the blind is *resting inside* the zone without being believed latched also does nothing: neither direction points toward a zone it already sits in, and there are no slats to step. Use the long press or the tilt control to get out of that state.

Stepping naturally crosses the zone boundaries: an up step at the open edge of the zone leaves tilt mode upward and resumes whole-blind control, and a down press from just above the zone enters it. Boundary crossings execute the full engage/disengage sequence rather than a small `tilt_step_pct` nudge.

The dashboard step helpers share rule 3 exactly (that is how a blind without a wall switch gets into tilt) but not rules 1 and 2's upward exit: they never stop a move and never leave tilt.

## Position And Restart Behavior

The application does not persist state across restarts. After Home Assistant or AppDaemon restarts, it re-establishes a known-safe state from the position reported by the underlying blind controller:

- If the reported position is clearly **outside** the tilt zone, the blind cannot be latched, so the application resumes whole-blind control from that position.
- If the reported position is **inside or near** the tilt zone, the latch state is ambiguous, so the application issues a full `cover.open_cover` (it sends the open command rather than a concrete target position) to drive fully open, then treats itself as being at `100%` with tilt mode off.

This upward-only recovery rule protects the Gradhermetic mechanism from accidental extra downward movement while the blind may already be in or near the tilt zone.

The same protection applies during normal operation, not just at restart. The application tracks the latch as one of three states — **latched**, **released**, or **unknown** — and only a completed entry sequence establishes "latched". It falls back to "unknown" whenever a sequence is interrupted part-way, the underlying cover becomes unavailable, or the blind moves without being told to; and it clears to "released" whenever the blind comes to rest clearly outside the `[tilt_zone_lower_pct - tilt_zone_epsilon_pct, tilt_zone_release_pct]` band, where a latched mechanism cannot be.

Any command that would drive the blind downward while the latch is not known to be released first drives fully open to release it, then descends. A blind that is *known* released descends straight away — closing right after leaving tilt mode, for instance, costs no detour.

This keeps the mechanism safe even if the application's belief was disturbed by an interrupted tilt sequence or by a command sent directly to the underlying cover.

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

  # Clearance margin for crossing a zone edge cleanly. Must be at least 1.0, so the rounded command
  # the actuator receives differs from the edge it has to clear.
  tilt_zone_epsilon_pct: 2.0

  # Optional. Real travel percent the blind must reach for the latch to genuinely release, measured
  # by raising the latched blind in small increments until it starts lifting as a whole instead of
  # only rotating the slats. Must be >= tilt_zone_upper_pct + tilt_zone_epsilon_pct (its default)
  # and <= 100. It also sets the top of the ambiguity band, so set_cover_position will not stop
  # anywhere between tilt_zone_lower_pct - tilt_zone_epsilon_pct and this value.
  tilt_zone_release_pct: 50.0

  # Optional. Virtual slat percent the entry sequence finishes on: 0 = slats closed (the position
  # the latching rise itself ends at, and the default), 100 = slats fully open. Set it a little
  # above 0 on a blind whose slats are not visibly open at the closed edge.
  tilt_enter_landing_pct: 20.0

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
