# Gradhermetic Cover Control

Gradhermetic Cover Control wraps an existing Home Assistant blind entity and exposes a user-facing cover that understands Gradhermetic **tilt mode** (slat-orientation control). See the [Gradhermetic downloads](https://gradhermetic.com/en/downloads) for the physical product. The physical Gradhermetic cover is a motor-driven blind with ordinary up and down travel; a specific sequence of movements latches the mechanism in and out of tilt mode. Once in tilt mode, further up and down movements change the slat orientation instead of the overall blind position.

The wrapped blind may be backed by any Home Assistant integration, as long as the underlying entity can report and accept regular blind position commands.

Implementation notes, runtime internals, and test instructions live in [IMPLEMENTATION.md](IMPLEMENTATION.md).

## User-Facing Contract

Each configured blind exposes one virtual cover entity, surfaced to Home Assistant as a template cover that forwards commands to the application. The virtual cover supports the standard Home Assistant cover services: `cover.open_cover`, `cover.close_cover`, `cover.stop_cover`, `cover.toggle`, and `cover.set_cover_position`.

Tilt mode is toggled by firing a `gradhermetic_command` event with `command: set_tilt_mode` and `enabled: true|false` — `true` enters tilt mode, `false` leaves it. This is the Home-Assistant-facing entry point (usable from any script, button, or automation) because the template cover already speaks to the app over that event bus. Entering tilt mode moves the blind through the mechanical sequence required to engage slat control; leaving tilt mode returns the virtual cover to regular position control.

For step and tilt from the UI, the app watches three `input_button` helpers per blind. `..._tilt` toggles tilt mode (enter/leave), and cancels an entry or exit that is still in progress. `..._step_up` and `..._step_down` follow one rule — the same one a KNX stop/step object follows — in priority order:

1. **If anything is moving** — a sequence the app started, or the blind reported travelling — **stop it.**
2. **Otherwise, if the mechanism is latched, step the slat angle** by `tilt_step_pct` (the real travel one slat step moves the blind), clamping at both zone edges. The helpers never step *out* of tilt at the open edge; leaving is the tilt helper's job.
3. **Otherwise, step the blind's height** by `height_step_pct` of real travel. A step that would land inside the ambiguity band continues to the band edge ahead of it, so the band is crossed in one press rather than being a place the blind gets stuck in front of. A step down while the latch might be engaged pays the full-open release first, like every other descent.

A step press never enters tilt mode. A press with nowhere to go — at a travel limit, at a slat edge, with no position known — is logged and does nothing else.

The guiding principle for every control surface is **up = more light, down = less light** — applied to the whole blind's height when outside the tilt zone, and to the slat angle when inside it.

The app publishes two entities of its own per blind, needing no helper declared for either:

- `sensor.gradhermetic_<id>_position` — the position the virtual cover displays, which the template cover reads. It carries a `motion` attribute (`opening` / `closing` / `idle`) the template cover's state template reads, so the cover shows travel as it happens.
- `binary_sensor.gradhermetic_<id>_tilt_mode` — `on` while slat control is engaged, `off` otherwise.

The second exists because nothing in Home Assistant can work it out: a real position inside the tilt zone is neither necessary nor sufficient for the mechanism being latched, which is the whole reason the app event-sources a latch belief rather than deriving one from the position. Both are written together, from the same moment, so the flag always says which scale the position beside it is on — a slat angle or a height. That is what lets a dashboard show which mode a blind is in, and what a toggle control should read to label itself: `..._tilt` is a button, and a button has no state to show.

Both are republished after every event that changes what they show — every position report during travel included, not only the end of a move — and the position sensor goes `unavailable` (rather than holding a stale number) whenever the real cover's position is unreadable. While an entry or exit sequence is running the cover shows height mode and the real height climbing to the top and back; it switches to the slat scale only once the mechanism has actually latched.

### Outside tilt mode

The standard cover services target the full blind travel range:

- `cover.open_cover` opens the blind fully (`100%`).
- `cover.close_cover` closes the blind fully (`0%`).
- `cover.set_cover_position` moves to the requested absolute position, except that a target landing *inside* the tilt zone's ambiguity band (between `tilt_zone_lower_pct - tilt_zone_epsilon_pct` and `tilt_zone_release_pct`) is snapped outward to the nearer edge of that band — or to the far edge when the nearer one is where the blind already rests, so a slider dragged into the band always moves the blind. Rising into the band would silently engage the latch while the application believed it was still doing height control, so whole-blind moves stay clear of it. The reported position is the snapped value.

  The band's upper end is the height at which the latch genuinely releases, because a latched-but-not-yet-released mechanism can be resting anywhere below it. Configuring a `tilt_zone_release_pct` well above the zone therefore widens the range of heights the blind refuses to stop at — with the default (`tilt_zone_upper_pct + tilt_zone_epsilon_pct`) the adjustment is a couple of percent of travel, but a blind that only releases much higher up will skip past more than that.

  A rise that ends *exactly* on that upper end — from below the zone, or from a latch belief that is not a known release — leaves the latch belief **unknown** rather than released. The rise may have latched the mechanism on its way across the lower edge and reached the release height with no margin at all; an actuator settling a percent short, or a percent of calibration error, leaves it latched while the feedback says it arrived, and only the top limit switch can tell. The next downward command therefore drives fully open first, and the belief clears itself as soon as the blind rests clear of the band.

### Inside tilt mode

Once latched, the same services control slat orientation within the narrow **tilt zone** between `tilt_zone_lower_pct` and `tilt_zone_upper_pct`:

- `cover.open_cover` orients the slats perpendicular to the window (most light) — the blind sits at `tilt_zone_lower_pct`.
- `cover.close_cover` orients the slats parallel to the window (least light) — the blind sits at `tilt_zone_upper_pct`.
- `cover.set_cover_position` interpolates between these two ends: `100%` maps to `tilt_zone_lower_pct` (slats fully open / perpendicular) and `0%` maps to `tilt_zone_upper_pct` (slats fully closed / parallel).

Note the inversion relative to normal travel: inside the tilt zone a *higher* absolute blind position means *more closed* slats.

Virtual `100%` targets `tilt_zone_lower_pct` exactly, so configure that setting as the height at which the slats are genuinely fully open: fully open on the slider then means fully open on the blind. See [Calibrating `tilt_zone_lower_pct`](#calibrating-tilt_zone_lower_pct) for why this edge needs no clearance margin of its own.

That inverted **virtual slat scale is only what the cover entity's position slider shows while tilt mode is engaged.** Every percentage in the YAML configuration — the zone edges, the clearance margin, the release height, the entry landing and the slat step alike — is real blind travel, the numbers the underlying actuator reports and accepts.

## Entering And Leaving Tilt Mode

The mechanism only latches when a full down-then-up motion is performed across the lower edge of the zone. A second hardware fact shapes the sequence just as much: **the percentages are only reliable when the sequence starts from the fully open position.** The actuator's reported position cannot be trusted to match the blind's true physical position unless the move is referenced from the top limit, and the tilt zone is only a few percent wide — so a dip aimed from an unreferenced height may not clear the lower edge at all.

Entering tilt mode is therefore a single sequence, run from wherever the blind happens to be:

1. Drive fully open with `cover.open_cover`. Sending the command rather than a target position makes the actuator run against its own limit switch, which re-references it. This step is skipped only when the blind already reports being fully open.
2. Move down to `tilt_zone_lower_pct - tilt_zone_epsilon_pct` (dip below the lower edge). Starting from fully open this is a pure descent, so it cannot engage the latch on the way down.
3. Move up to `tilt_zone_upper_pct`. The upward crossing of the lower edge latches the mechanism in tilt mode, with the slats parallel (closed).
4. Move to the slat angle given by `tilt_enter_landing_pct` — an absolute real position that must lie inside the zone (`tilt_zone_lower_pct` = slats fully open, `tilt_zone_upper_pct` = slats closed). This is one more small in-zone move and is omitted when the landing rounds to the position step 3 already reached. It defaults to `tilt_zone_upper_pct`, the closed edge the latching rise ends on anyway — i.e. no fourth step at all.

Step 4 exists because the latching rise necessarily ends with the slats fully closed, and on a real blind the slats often do not visibly open until a couple of percent below `tilt_zone_upper_pct` — so an entry that lands exactly on the closed edge looks like it did nothing. Set `tilt_enter_landing_pct` to the height at which the slats are as open as you want tilt mode to start; on a zone of `[29, 34]`, for instance, `32` is a slightly-open landing. Every entry goes through this same sequence: the tilt helper, the KNX slat-mode address, the event and the service.

Asking to enter while an entry is already running, or to leave while an exit is, is a no-op rather than a restart. Asking to leave while an entry is running stops the entry, and asking to enter while an exit is running stops the exit: in both cases the blind halts where you can see it, rather than letting a sequence you have just contradicted run to completion behind you. Every one of these no-op or redirected requests is logged with its reason. The tilt helper and the KNX slat-mode address toggle, and a toggle during either sequence cancels it.

Both also read the mode the cover is *displaying* rather than the application's internal latch belief, so the tilt control always does what its label says. The two differ while a whole-blind move runs from slat mode — a long press up, for instance — because such a move may release the latch, so the cover switches to Height Mode at once while the belief only settles when the move finishes.

To leave tilt mode:

- Drive fully open with `cover.open_cover`. Leaving is always an upward move; the application never drives downward to disengage.

  Two heights are in play, and they are deliberately different. Where the blind **stops** is the top limit: leaving slat mode is a request to go back to controlling the blind as a whole, and stopping a few percent above the zone would instead park it in the ambiguity band with the slats still shut — a resting place nobody asks for. Running against the limit switch also re-references the actuator on the way, and no settling error can leave a limit switch short.

  What the move has to **reach** to have done its job is still `tilt_zone_release_pct` — the height at which the mechanism genuinely lets go, defaulting to `tilt_zone_upper_pct + tilt_zone_epsilon_pct`. The exit is considered complete the moment the blind reports that height or above, because from there the latch has provably released. Accepting there rather than at `100` also means a blind that settles a percent below its own top limit still completes the exit honestly.

Whenever the latch state is instead **uncertain** — after an interrupted sequence, a restart, or a move the application did not command — a release cannot rely on a reported percentage either, so it is a full `cover.open_cover` as well. This is also how the application re-references itself after a restart, lazily, when a move first needs it (see "Position And Restart Behavior").

`tilt_zone_epsilon_pct` is the clearance margin used to cleanly cross the lower edge when engaging and the upper edge when disengaging. It must be at least one whole percent, so the rounded command the actuator receives is distinct from the edge it has to clear.

### Calibrating `tilt_zone_release_pct`

`tilt_zone_epsilon_pct` only has to carry the *reported* position clear of the upper edge; the latch itself may need considerably more real travel before it disengages. To measure the difference: latch the blind into tilt mode, then raise it in small increments (a percent or two at a time) and watch it. While the mechanism is still latched the movement only changes the slat angle; the height at which the blind starts lifting *as a whole* is the release height. Set `tilt_zone_release_pct` to that value (rounded up).

Until you have measured it, set it conservatively a few percent above the zone rather than leaving it at the default. An exit that does not physically release is the one failure the design cannot absorb: the application commits to believing the mechanism is free, so the next downward command descends straight away — on a blind that is still latched. Setting the value too *high* costs nothing: the exit travels to the top limit regardless, so the only effect is a slightly wider band of heights that `cover.set_cover_position` refuses to stop at.

Note that this height is no longer where leaving tilt mode *stops* — the exit runs to the top limit either way. It is what tells the application the latch has let go, and how far up the ambiguity band reaches.

Entering tilt mode costs an upward trip to fully open first. That is deliberate: rising is the one direction that is always safe, and the top limit is the only position the actuator cannot be wrong about.

### Calibrating `tilt_zone_lower_pct`

Set it to the height at which the slats are fully open, and no higher. Virtual `100%` targets this edge exactly, so it is what makes the slider's "fully open" mean the angle the slats are actually built to reach.

It is the one landmark in the design with no clearance margin of its own, which looks like an oversight next to the dip and the release height. It is not. The rule a margin here would pad is "never travel below the lower edge while latched", and what that rule exists to prevent is the application *aiming* a descent through the slat range — a close, a long press down — on an engaged mechanism. It never does; that is invariant L1, and the planner sweep proves it over every plan the planner can emit.

What a margin would actually buy is padding against the actuator overshooting its own setpoint by a fraction of a percent, and the only way to buy it is to raise this number, which makes fully open stop short of fully open. That trade is not worth making. The cost is visible every time you open the slats, while the overshoot is a few tens of milliseconds of travel against a stop the slats have already reached. And an actuator drifting far enough for it to matter has already put every slat angle, the entry landing included, somewhere other than where the application believes — so this edge is not a weak point, it is simply where a strict check notices the drift first.

## Wall-Button (KNX) Control

The application can be driven by a two-button KNX wall switch (an up button and a down button, each distinguishing a short press from a long press). This maps onto the two standard KNX blind communication objects, plus one address of the application's own for slat mode:

- A **"Move"** group address that receives **long** presses (`knx_move_address`).
- A **"Stop/Step"** group address that receives **short** presses (`knx_step_address`).
- A **slat-mode** group address that toggles tilt mode (`knx_tilt_address`).

For move and step the telegram's value selects the direction (up = more light, down = less light). These group addresses are surfaced to the application as `knx_event`s on the Home Assistant event bus.

These must be group addresses **nothing else listens on** — in particular not the actuator's own move objects. A wall button linked directly to those drives the actuator itself, and no amount of listening lets the application mediate the press; the button has to be linked to a dedicated address instead, which the application then acts on by calling the ordinary cover services.

The slat-mode address is a **stateless trigger, not a mode level**: it acts on a `1` and ignores a `0`, and what it does is read off the application's own latch belief — the same toggle the `..._tilt` dashboard helper performs. Two consequences for how the pushbutton is parameterized:

- It should be configured to **send the ON telegram only**. A momentary button parameterized as a two-state switch sends `1` on press and `0` on release; ignoring the `0` is what keeps one physical press from toggling twice and cancelling itself out.
- A stateful "1 = enter, 0 = leave" object would have been wrong regardless, because the wall switch cannot see tilt mode changing by any other route. A long up press leaves tilt mode, and a switch holding its own idea of the mode would be inverted from then on.

### Long press — jump to an extreme

- **Long up** drives the blind fully open (`100%`). If it is currently in tilt mode, this naturally leaves tilt mode — the exit is a full open anyway, so the two are the same move.
- **Long down** drives the blind fully closed (`0%`). Unless the latch is known to be released, the blind first drives fully open to release it — the latch only releases upward — and then descends.

### Short press — stop, or step in the more-light / less-light direction

A short press is evaluated in this priority order:

1. **If anything is moving, stop it.** (This matches the native KNX "Stop/Step" behavior.)
2. **Otherwise, if the mechanism is latched, step the slats** by `tilt_step_pct` of real travel — up steps toward open (more light), down steps toward closed (less light). An up step at the open edge leaves tilt mode upward and resumes whole-blind control: a two-button switch has no other way out.
3. **Otherwise, step the height** by `height_step_pct` of real travel, exactly as the dashboard step helpers do — skipping the ambiguity band in the direction of travel, and paying the full-open release first when stepping down from an uncertain latch belief.

A short press never enters tilt mode; the slat-mode address does that. The dashboard step helpers follow the same rule except for rule 2's upward exit: they never leave tilt, since the dashboard has a tilt control of its own.

Because every command reaches the actuator through this application, the application never relies on the actuator's own stop/step object either: a stop reaches the actuator only while the blind may actually be travelling — the controller reports it moving, or a command has gone out and no settled report for it has come back yet. Once the controller has reported the blind at rest, no stop is sent, including through the ten-second recheck after it settles short of a target: the blind has already stopped by itself. On a KNX actuator without a dedicated stop object, Home Assistant carries `stop_cover` on the step object, which would *nudge* an idle blind instead of stopping it.

That reasoning needs the controller to report motion in the first place. An integration that publishes positions but never an `opening`/`closing` state reports the blind as not moving throughout a move, so treating its reports as "stopped" would throw the stop away while the blind was still travelling. The application therefore remembers whether the controller has ever reported motion, and until it has, it falls back to sending the stop whenever a movement is outstanding.

## Position And Restart Behavior

The application does not persist state across restarts. After Home Assistant or AppDaemon restarts, it seeds its belief from the position the underlying blind controller reports — and moves nothing:

- If the reported position is clearly **outside** the tilt zone's ambiguity band, the blind cannot be latched, so whole-height control resumes from that position.
- If the reported position is **inside** that band, or unreadable, the latch state is ambiguous, so the latch belief starts out unknown. **The blind does not move.** The actuator is re-referenced lazily instead, by the first action that actually needs a trusted position: every descent, and every entry into tilt mode, begins with a full `cover.open_cover` — which is also the release a latch that *might* be engaged gets, since a short rise to a merely reported height cannot be trusted from an uncertain belief. `cover.open_cover` itself, and a long up press, re-reference on their own — they run the actuator against its top limit switch anyway.

Deferring the reference this way costs nothing in safety: every move that could harm the mechanism still buys it first. What it buys is that a restart — after a power cut, say — never raises the blind unprompted in the middle of the night.

One consequence is worth knowing. While the blind rests inside the band with the latch belief unknown, the step controls still work, but asymmetrically: a step up rises out of the band directly (rising is always safe), while a step down — like every descent from an uncertain belief — drives fully open first and then descends.

The latch belief works the same way during normal operation, not just at restart. The application tracks the latch as one of three states — **latched**, **released**, or **unknown** — and only a completed entry sequence establishes "latched". It falls back to "unknown" whenever a sequence is interrupted part-way, the underlying cover becomes unavailable, the blind moves without being told to, or a height move rises by position command to exactly the release height; and it clears to "released" whenever the blind comes to rest clearly outside the `[tilt_zone_lower_pct - tilt_zone_epsilon_pct, tilt_zone_release_pct]` band, where a latched mechanism cannot be.

Any command that would drive the blind downward while the latch is not known to be released first drives fully open to release it, then descends. A blind that is *known* released descends straight away — closing right after leaving tilt mode, for instance, costs no detour.

This keeps the mechanism safe even if the application's belief was disturbed by an interrupted tilt sequence or by a command sent directly to the underlying cover.

## YAML Configuration

**Every percentage below is real blind travel** — the position the underlying actuator reports and accepts. The inverted virtual slat scale (`0` closed … `100` open) never appears in configuration; it only shows up on the cover entity's own position slider while tilt mode is engaged.

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
  # the actuator receives differs from the edge it has to clear. The band it defines around the zone
  # (tilt_zone_lower_pct - tilt_zone_epsilon_pct up to tilt_zone_release_pct) must stay strictly
  # inside 0..100: a blind resting on either end stop has to count as clearly unlatched.
  tilt_zone_epsilon_pct: 2.0

  # Optional. Real travel percent the blind must reach for the latch to genuinely release, measured
  # by raising the latched blind in small increments until it starts lifting as a whole instead of
  # only rotating the slats. Must be >= tilt_zone_upper_pct + tilt_zone_epsilon_pct (its default)
  # and < 100. It also sets the top of the ambiguity band, so set_cover_position will not stop
  # anywhere between tilt_zone_lower_pct - tilt_zone_epsilon_pct and this value.
  tilt_zone_release_pct: 50.0

  # Optional. Real travel position the entry sequence finishes on, which being a slat position must
  # lie inside the zone: tilt_zone_upper_pct = slats closed (the position the latching rise itself
  # ends at, and the default), tilt_zone_lower_pct = slats fully open. Set it a little below the
  # upper edge on a blind whose slats are not visibly open at the closed edge.
  tilt_enter_landing_pct: 42.8

  # Real travel percent one slat step moves the blind, for short presses while inside the tilt zone.
  # Because the actuator reports integer positions, a step below one whole percent would command a
  # position that rounds back to the current one and move nothing at all, so tilt_step_pct must be
  # >= 1.0; it must also be <= the zone's width (tilt_zone_upper_pct - tilt_zone_lower_pct), since a
  # step larger than the whole zone is meaningless. A 6% zone therefore yields at most six usable
  # slat positions. The app rejects an out-of-range step at startup.
  tilt_step_pct: 1.2

  # Optional. Real travel percent one step button press moves the blind while it is not in slat
  # mode. Same lower bound as tilt_step_pct, for the same reason; at most 100. Defaults to 2.0.
  height_step_pct: 2.0

  # Optional KNX wall-button group addresses, each of which nothing but Home Assistant may listen
  # on. The "move" address receives long presses; the "step" address receives short presses, with
  # the direction (up/down) carried by the telegram value. The "tilt" address toggles slat mode: it
  # is a trigger rather than a level, acting on a 1 and ignoring a 0.
  knx_move_address: "1/2/3"
  knx_step_address: "1/2/4"
  knx_tilt_address: "1/2/5"
```
