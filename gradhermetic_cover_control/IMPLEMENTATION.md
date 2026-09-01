# Gradhermetic Cover Control Implementation

This app is a pure core with a thin AppDaemon adapter around it. Every decision — which sequence to
run, whether a waypoint has been reached, when to give up on a move — is made in code with no I/O in
it, so all of it is testable without an AppDaemon installation.

| Module | Responsibility | Purity |
|---|---|---|
| `geometry.py` | `Zone`: the virtual↔real mapping, band and zone predicates, the named dip/release targets, band snapping, and all validation of the configured numbers | pure |
| `planner.py` | `plan(zone, belief, intent) -> Plan`: every movement sequence and every latch guard, plus `check_plan`, which restates the safety argument as executable invariants | pure |
| `executor.py` | Drives one `Plan`: step activation and arrival, settle-timer decisions, stall detection. Consumes feedback and timer events, emits `Action`s | pure |
| `logic.py` | `GradhermeticCoverLogic`: holds the belief and routes events to intents, composing planner and executor. The adapter talks only to this | pure |
| `gradhermetic_cover_control.py` | AppDaemon adapter: listeners, service calls, `set_state`, notifications, timers. Makes no decisions | I/O |
| `config.py` | Parses `apps.yaml` args; the numeric rules are delegated to `geometry` | pure |
| `runtime.py` | The settle-timer handle and the command rate limiter | state |

Each app instance manages exactly one blind.

The adapter's whole vocabulary is the `Action` list the core returns: `move_to` / `open_full` /
`close_full` / `stop` become real-cover service calls, `publish_position` becomes a `set_state`,
`arm_settle_timer` / `cancel_settle_timer` become `run_in` / `cancel_timer`, and `notify` becomes a
persistent notification. What is left in the adapter is transport only: listening and filtering,
gating commands until startup recovery has run, decoding KNX telegrams and button presses, the
command rate limit, and the callback `try`/`except` boundary.

## Transport: template cover, not MQTT

The virtual cover is surfaced to Home Assistant **without MQTT** — the config repo has no broker. A
small template cover (defined in the HA config) does two things and nothing else:

- Its `open_cover` / `close_cover` / `stop_cover` / `set_cover_position` actions fire a
  `gradhermetic_command` event that this app listens for.
- Its `position_template` reads `sensor.gradhermetic_<virtual_id>_position`, which the app publishes
  itself via `set_state` when a movement plan completes. No `input_number` helper is involved — the
  app owns the sensor.

Step and tilt controls are exposed the same broker-free way: three name-only `input_button` helpers
(`..._step_up`, `..._step_down`, `..._tilt`) that carry no logic. The app listens for each press and
routes it into the logic engine (see "Virtual Cover Wiring").

All decision-making stays in `logic.py`; the template and helpers contain no logic. The
`set_cover_position` value, the open/close mapping, latching, and recovery are all decided by the
Python app.

## Belief

The app persists nothing across restarts, so everything it does follows from three tracked facts:

- `last_position` — the blind's real travel position (0-100) from controller feedback, or `None`
  once the cover becomes unavailable. An unknown position makes every guard conservative
  automatically.
- `latch` — `LATCHED` / `UNLATCHED` / `UNKNOWN`. This is **event-sourced, not derived from the
  position**: a position inside the band is neither necessary nor sufficient for being latched, so a
  positional test cannot tell "known released" from "no idea", and that distinction is what decides
  whether a descent needs a release first.
- `is_moving` — whether the blind is travelling, from controller feedback.

The latch transitions, in full:

- → `LATCHED`: a completed enter sequence, and nothing else.
- → `UNLATCHED`: a completed plan that ends released, or feedback placing the blind clearly outside
  the `[lower - epsilon, release_target]` band, where a latched mechanism cannot rest.
- → `UNKNOWN`: startup with the position unknown or inside the band; a plan interrupted (stopped,
  replaced or stalled) part-way; externally-caused motion ending inside the band; the cover becoming
  unavailable.

One refinement keeps that last rule from being needlessly destructive: a plan that never leaves
`[lower, upper]` — neither in what it targets nor in what it commands — is pure slat rotation. It starts inside the zone — that is what being
latched means — and moves monotonically to another in-zone target, so it can neither engage the
latch (which needs a rise across the lower edge from below) nor release it (a rise above the upper
edge). Interrupting one therefore leaves a confident `LATCHED` belief intact. Without that, stopping
a slat move would drop the blind out of tilt mode and make the next close drive it fully open.

`in_tilt` (slat control applies) is `latch == LATCHED`; `may_be_latched` (a descent needs a release
first) is `latch != UNLATCHED`. Those two derived forms are the only ones the guards use.

## Movement Plans

Every intent compiles to an ordered list of **steps**, each with an explicit satisfaction predicate
stated in the integer domain the actuator actually speaks — commands are rounded to whole percent
and a KNX actuator reports the setpoint value it reached, so exact integer comparison is the honest
test:

- `MoveTo(target)` — satisfied when `round(position) == round(target)`. The `open_full` and
  `close_full` variants send `cover.open_cover` / `cover.close_cover` rather than a position, so the
  actuator drives against its own limit switch.
- `RiseToAtLeast(target)` — satisfied when `round(position) >= round(target)`. Used only for the
  tilt exit, where anything above the release target is equally good.

A step's `target` is its *satisfaction* threshold. An optional `command_pct` names a different
position to actually send, defaulting to the target; only the tilt exit uses it (see "Canonical
Sequences"). Everything about arrival — skip-if-satisfied, `on_feedback`, the settle timer's
deviation acceptance — keeps measuring the target, and only the outgoing command follows
`command_pct`. A stall message names the target too, since that is the number the resting position
should be compared against.

The step lifecycle is where the timing correctness lives:

1. **Activation.** If the predicate already holds and the blind is settled, the step is *skipped* —
   no command is sent and the next step activates immediately. A plan can therefore never begin with
   a command the actuator will never acknowledge, and so can never wait on feedback that will never
   arrive. A step is *not* skipped while the blind is still travelling: passing through a position
   is not resting on it, which matters when a command replaces a plan mid-move.
2. Otherwise the command goes out and the settle timer is armed.
3. **Arrival.** The step completes only on settled feedback satisfying the predicate. Because
   activation guaranteed the predicate did *not* hold when the command went out, a duplicate or
   delayed report carrying the pre-command position can never satisfy it — however small the step
   was. (A 20% slat step in a 6% zone is only 1.2 real percent, so this matters.)
4. **The settle timer fires.** Still moving → re-arm, because this is an inactivity timeout and not
   a travel-time cap. Settled and satisfied → complete, which covers an actuator that reported only
   its final state or none at all. Settled within `DEVIATION_TOLERANCE_PCT` of a `MoveTo` target →
   accept with a logged warning, because real actuators occasionally stop a percent off. Otherwise,
   or when the position has become unreadable → stop the blind, drop the plan and notify.
5. **Completion.** The plan's terminal latch belief is committed, the settle timer is cancelled, and
   the virtual position is published to `sensor.gradhermetic_<virtual_id>_position` — derived from
   the position the blind actually reports rather than from the setpoint, so the published value and
   the app's own belief can never disagree.

A command arriving while a plan is in flight **replaces** it. The replacement is planned from the
belief as it will be *after* the interruption, so it re-derives every safety guard; an intent that
plans to nothing (a slat step outside tilt, say) leaves the running plan alone.

## Canonical Sequences

- **Enter tilt** — `open_full`, then `MoveTo(lower - epsilon)`, then `MoveTo(upper)`, then
  optionally `MoveTo(virtual_to_real(landing))`. One sequence, correct from any start. The leading
  full open re-references the actuator at its limit switch (see the README on why the percentages
  are only reliable from there) and makes the dip a pure descent, which cannot latch.

  The latching rise can only end at the upper edge, so any other landing is one more in-zone slat
  move. Who chooses it depends on the caller: a deliberate `set_tilt_mode` enter passes the
  configured `tilt_enter_landing_pct`, while a wall-button or step-button entry passes the
  directional near-edge rule instead (from above → closed edge / virtual 0, from below → open edge /
  virtual 100) — the press already says which end the user was reaching for. The fourth step is
  dropped when its landing rounds to the same integer command as the upper edge, because a command
  that repeats the current setpoint moves nothing.
- **Leave tilt** — `RiseToAtLeast(release_target)` commanded at `min(100, release_target + 2)`,
  available only from a confident `LATCHED` belief. `release_target` is `tilt_zone_release_pct`, or
  `upper + epsilon` when that is not configured.

  The overshoot is the whole point of the step carrying a command distinct from its target. `epsilon`
  is sized to carry the *reported* position clear of the upper edge, which says nothing about how far
  the mechanism has to travel to disengage; and commanding exactly the acceptance threshold means an
  actuator settling a percent low satisfies `>=` on a rise that physically fell short, leaving the
  app confidently — and wrongly — believing it is released. `planner.EXIT_OVERSHOOT_PCT` is
  deliberately equal to `executor.DEVIATION_TOLERANCE_PCT`, so any settling the executor would
  forgive a `MoveTo` for still satisfies this step on its own; the constant is duplicated rather
  than imported because the planner must not depend on the executor.
- **Guarded descent** (close, long-down, a descending `set_position`) — when `may_be_latched`,
  prefix `open_full`: an uncertain latch belief also means an uncertain calibration, so a short rise
  to a merely *reported* release height cannot be trusted. When the latch is known released,
  descend directly.
- **Normal-mode `set_position`** — snap the target clear of the band, then one `MoveTo`, guarded
  when it descends or the position is unknown.
- **In-tilt moves** — a single `MoveTo` inside `[lower, upper]`.
- **Recovery** — a single `open_full`.

```text
        enter: open fully, down to (lower - epsilon), up to upper, then to the landing
   NORMAL  ───────────────────────────────────────────────────────────►  TILT
 (height control)                                                  (slat control, latched)
      ▲                  leave: up to release_target (commanded +2)          │
      └───────────────────────────────────────────────────────────────────────┘
                          (disengage is always upward)
```

## Invariants

`planner.check_plan` runs on every plan before it executes. A violation disables the blind and
notifies; it should be unreachable, and the tests exist to prove it:

- **N1** — in normal mode nothing lands strictly inside the band `(lower - epsilon, release_target)`.
- **T1** — slat targets lie within `[lower, upper]` and are only planned from a `LATCHED` belief.
- **L1** — a descent below `lower` is preceded by a full open unless the latch is known released.
- **E1** — the latch belief is only established by the canonical enter sequence: full open, a dip
  clear of the lower edge, the latching rise to the upper edge, and an optional fourth step to any
  target *inside the zone* (the landing). It starts from the upper edge, so it can only descend to
  another slat angle — never across an edge.
- **X1/R1** — leaving tilt and recovering are upward-only, and every release from an uncertain
  belief is a full open rather than a rise to an unreferenced percentage. The tilt exit must also
  reach at least `release_target` and command at least as high as it accepts.

N1, T1 and L1 — and `can_change_latch` with them — check both the satisfaction target and the
commanded position of every step, since the hazard is where the blind physically travels and the two
are allowed to differ. Today only the tilt exit makes them differ, and it does so upward.

## Tilt-Zone Math

Outside the zone the virtual cover maps one-to-one to the real travel position (up = open = 100 =
more light). Inside the zone the mapping is inverted between the edges:

```text
real   = upper - (virtual / 100) * (upper - lower)
virtual = (upper - real) / (upper - lower) * 100
```

With `upper = 44`, `lower = 38`, `epsilon = 2`:

- virtual `100` → real `38` (slats open / perpendicular / most light).
- virtual `0` → real `44` (slats closed / parallel / least light).
- virtual `50` → real `41`.
- entering dips to `lower - epsilon = 36`, then rises to `44` to latch, then moves to
  `virtual_to_real(tilt_enter_landing_pct)` if that is not `44` as well.
- leaving rises to `release_target` — `upper + epsilon = 46` unless `tilt_zone_release_pct` says
  otherwise — commanded two percent higher than that.

Because the zone is narrow (6% here) and KNX actuators report integer positions, the zone holds only
about `span + 1` distinct slat positions (~7 for a 6% zone). A slat step must therefore map to at
least one whole reported percent of real travel, otherwise the rounded position command repeats the
current position and the blind never moves. Config validation enforces
`tilt_step_pct >= 100 / (upper - lower)` (≈16.7 here) so every step advances the actuator, and
`tilt_zone_epsilon_pct >= 1` so the dip and release targets round to integers distinct from the
edges they must clear. The two optional settings are validated here too:
`tilt_zone_release_pct` must be between `upper + epsilon` and `100` (below the clearance it would
not even carry the reported position out of the zone), and `tilt_enter_landing_pct` must be a
virtual percentage in `[0, 100]`. All of it lives in `geometry.Zone`, which validates on
construction — `config.py` only checks that each number is present (or, for these two, absent),
numeric and in range.

The ambiguity band runs `[lower - epsilon, release_target]`, and `band_high` is *defined* as
`release_target` rather than merely coinciding with it: a mechanism that is latched but has not yet
been released can physically be resting anywhere up to the height at which it lets go, so that is
exactly how far "latched cannot be ruled out" reaches. Everything derived from the band inherits a
configured release height automatically — `in_band`, `snap_normal_target`, startup recovery, and the
feedback rule that clears a latch belief.

Outside tilt, a `set_cover_position` target landing strictly inside the band `(36, 46)` here is
snapped to the nearer band edge, ties rising. Rising into the band from below silently engages the
latch, so a whole-blind move that aimed there would leave belief and reality diverging; snapping
costs a couple of percent of travel and makes "normal mode never targets the band interior" an
invariant (N1) instead of a hazard. Raising `tilt_zone_release_pct` raises `band_high` with it, so
the snap grows to cover every height at which the blind might still be latched — that widening is
the deliberate price of an exit that actually releases.

## KNX Wall-Button Handling

Two dedicated group addresses drive the app as `knx_event`s (telegram value `0 = up / more light`,
`1 = down / less light`, matching the repo convention):

- **Move address** — long presses. Long up drives fully open (leaving tilt naturally); long down
  drives fully closed (driving fully open first unless the latch is known released, since the latch
  releases only upward).
- **Step address** — short presses, evaluated in priority order:
  1. If the blind is moving, stop it.
  2. Otherwise, if latched, step the slats by `tilt_step_pct` (up toward open, down toward closed).
     An up step at the open edge leaves tilt upward and resumes whole-height control.
  3. Otherwise (idle, not latched), enter tilt when the press points toward the zone: a down press
     from above enters at the most-closed edge; an up press from below enters at the most-open edge.
     A press pointing away does nothing (long press covers the extremes), and so does a press made
     while resting *inside* the zone without a latch belief — neither direction points toward a zone
     the blind already sits in, and there are no slats to step.

## Virtual Cover Wiring

Commands reach the app as a `gradhermetic_command` event carrying `virtual_id` and `command`
(`open` / `close` / `stop` / `set_position` with `position`, or `set_tilt_mode` with `enabled`). The
app filters by `virtual_id` and routes each to the logic engine. Position is reflected back with
`set_state` on `sensor.gradhermetic_<virtual_id>_position`, which the template cover displays.

Step and tilt reach the app as `input_button` presses. The app watches
`input_button.gradhermetic_<virtual_id>_step_up` / `_step_down` and routes each to `on_slat_step`
(up/down), and `..._tilt`, which toggles tilt mode (`on_set_tilt_mode` with the negation of the
current `in_tilt`). `on_slat_step` ignores a press outright while a plan is in flight or the blind
is travelling, and otherwise splits on the latch belief: latched, it steps the angle by
`tilt_step_pct` and clamps at both zone edges; not latched, it plans
`INTENT_ENTER_TOWARD_ZONE` — the wall button's rule 3, near-edge semantics and all.

That second half was added because a blind with no KNX wall switch had no directional way into tilt
from the dashboard at all, and the buttons simply did nothing. `on_slat_step` still differs from
`on_knx_short` in the other two rules: it never stops a move in flight, and it never *leaves* tilt
by stepping up at the open edge. The UI has `..._tilt` for that, and the configured
`tilt_enter_landing_pct` applies only to that deliberate entry — a directional press lands on the
edge the press pointed at.

Tilt mode is also toggled from Home Assistant with a `gradhermetic_command` event carrying
`command: set_tilt_mode` and `enabled: true|false` — this is the HA-facing entry point. A call whose
`enabled` is missing is ignored (rather than silently coerced to "leave tilt").

The app also registers `gradhermetic_cover_control/set_tilt_mode` via AppDaemon's `register_service`,
targeted by `virtual_id` (or the virtual `entity_id`, accepted as a string or a single-item list); a
call with neither applies to every instance. Note this lives in AppDaemon's namespace and is **not** a
Home Assistant service callable from HA scripts or the UI — use the event form from Home Assistant.

> Multi-instance note: every instance registers the same service name. If a future AppDaemon version
> does not multiplex a shared service name across apps, use the event form (or namespace the service
> per blind).

## Restart Behavior

State is **not** persisted across restarts. `RECOVERY_DELAY_SECONDS` after startup the adapter reads
the real cover's position and hands it to `logic.on_startup`, which seeds the belief and decides:

- position clearly **outside** the band (beyond `lower - epsilon` … `release_target`): the blind
  cannot be latched, so the belief starts `UNLATCHED` and whole-height control resumes from that
  position.
- position **inside** the band, or unknown: the latch state is ambiguous, so the belief starts
  `UNKNOWN` and the app drives a single `cover.open_cover` — an **upward-only** recovery that
  protects the mechanism from accidental downward movement near the tilt zone.

Commands are ignored until that has run: a command arriving in the recovery window would act on an
unseeded belief and could clobber the recovery plan.

## Safety Behavior

Each blind has a real-cover command rate limit. If the app sends more than `COMMAND_RATE_LIMIT`
commands within `COMMAND_RATE_WINDOW_SECONDS` (guarding against a plan whose waypoint is never
reached), the blind is disabled until AppDaemon restarts and a Home Assistant persistent notification
is created.

The `SETTLE_TIMEOUT_SECONDS` fallback timer only declares a stall — stopping the blind and raising an
obstruction notification — when the blind has **settled** short of its target. A move still reporting
motion when the timer fires is treated as merely long: the timer re-arms and waits, so a slow travel
never triggers a false stall. A pending plan whose position has become unreadable (the cover went
unavailable) is treated as a genuine stall rather than being left to hang silently. Healthy moves
never rely on the timer at all — the model tests assert every nominal flow completes without it
firing.

A plan that fails `check_plan` disables the blind and notifies. That is defence in depth against a
planner bug: the invariants are meant to be unreachable, so reaching one means the safe response is
to stop deciding anything for that blind until a human looks at it.

Unhandled callback exceptions likewise disable the blind until restart and create a persistent
notification. Wrapping callbacks in `try/except` is the one sanctioned exception to the project's
"let errors propagate" rule — it applies only at the AppDaemon callback boundary. Malformed external
input is not an app bug and does not go through it: a bad `set_position` value, a `set_tilt_mode`
without `enabled`, an unknown command, an unparseable KNX telegram, or a non-numeric reported
position are all logged and ignored.

## Home Assistant Wiring

No broker or add-on is required — everything runs through the AppDaemon HASS plugin already in use.
Three pieces live in the private HA config repo. The position sensor
(`sensor.gradhermetic_<id>_position`) needs no helper — the app publishes it.

### 1. Step/tilt trigger helpers (`input_buttons.yaml`)

Name-only buttons; all logic lives in the app.

```yaml
gradhermetic_living_room_step_up:
  name: Living Room Blind Step Up
  icon: mdi:chevron-up
gradhermetic_living_room_step_down:
  name: Living Room Blind Step Down
  icon: mdi:chevron-down
gradhermetic_living_room_tilt:
  name: Living Room Blind Tilt
  icon: mdi:angle-acute
```

### 2. Template cover (a dumb forwarder, no logic)

Modern Home Assistant configures template entities under the `template:` key, not `platform: template`
under `cover:`. If a `template:` include already exists in `configuration.yaml`, add this as another
list entry in that included file rather than a second `template:` key.

```yaml
template:
  - cover:
      - name: "Living Room Blind"
        unique_id: gradhermetic_living_room
        default_entity_id: cover.gradhermetic_living_room
        position: "{{ states('sensor.gradhermetic_living_room_position') | int(0) }}"
        open_cover:
          - event: gradhermetic_command
            event_data: {virtual_id: living_room, command: open}
        close_cover:
          - event: gradhermetic_command
            event_data: {virtual_id: living_room, command: close}
        stop_cover:
          - event: gradhermetic_command
            event_data: {virtual_id: living_room, command: stop}
        set_cover_position:
          - event: gradhermetic_command
            event_data: {virtual_id: living_room, command: set_position, position: "{{ position }}"}
```

`default_entity_id` pins the entity id to `cover.gradhermetic_<virtual_id>` independent of `name` —
`_service_targets_me` in the adapter and the dashboard tile both expect that exact id.

### 3. Dedicated KNX wall-button addresses

The move/step group addresses driving this app must be **input-only** — programmed in ETS so they do
not directly command the blind actuator. The app mediates every press and issues the actuator's
`position` commands itself; if the buttons also drove the actuator, tilt latching would be bypassed.

Expose those addresses as `knx_event`s by adding an `event:` block to the KNX config (this block does
not exist yet):

```yaml
# knx.yaml
event:
  - address: "2/6/0"   # Living Room Blind wall-button move (long press)
    type: "1.008"      # up/down
  - address: "2/6/1"   # Living Room Blind wall-button step (short press)
    type: "1.007"      # step
```

## Installation

This repository is used as a git submodule under `apps/public_apps` in the private Home Assistant
config repo. Register one instance per blind in `apps/apps.yaml`; AppDaemon resolves the module path
as `public_apps.gradhermetic_cover_control.gradhermetic_cover_control` from that location.

```yaml
GradhermeticLivingRoom:
  module: public_apps.gradhermetic_cover_control.gradhermetic_cover_control
  class: GradhermeticCoverControl

  real_cover: cover.living_room_blind
  virtual_id: living_room
  virtual_name: "Living Room Blind"

  tilt_zone_upper_pct: 44.0
  tilt_zone_lower_pct: 38.0
  tilt_zone_epsilon_pct: 2.0
  tilt_step_pct: 20.0

  # Optional; see the README for how to measure the release height and pick a landing angle.
  tilt_zone_release_pct: 50.0
  tilt_enter_landing_pct: 20.0

  knx_move_address: "2/6/0"
  knx_step_address: "2/6/1"
```

## Running Tests

From the `apps/public_apps` directory:

```bash
python3 -m unittest discover -s gradhermetic_cover_control/tests -t . -v
```

No AppDaemon installation is required, and the whole suite runs in well under a second.

| File | What it covers |
|---|---|
| `test_geometry.py` | Mapping round-trips, band and zone predicates at the exact edges, band snapping, every validation rule |
| `test_planner.py` | Golden sequences for every intent from representative starts; a sweep asserting every plan the planner can emit over the whole state space satisfies `check_plan`; hand-built plans proving each invariant rejects what it forbids |
| `test_executor.py` | Skip-if-satisfied, duplicate-feedback immunity, every settle-timer path (re-arm, accept, deviation, stall, unreadable), cancel-on-completion |
| `test_logic.py` | The event surface end to end, belief transitions, plan replacement, and named regressions for the four bugs the redesign removed |
| `test_config.py`, `test_runtime.py` | `apps.yaml` parsing and the command rate limiter |
| `test_adapter.py` | The AppDaemon layer against a fake `hass.Hass`: wiring, event filtering, the startup gate, malformed payloads, button edge detection, every `Action`'s translation, the rate limit and the error boundary |
| `simulator.py`, `test_model.py` | See below |

`simulator.py` is an independent ground-truth model of the mechanism, under the most pessimistic
latch semantics consistent with the hardware: any upward crossing of the lower edge from below
engages the latch, only a rise that actually reaches `release_target` releases it, and downward
travel never changes it. Releasing is modelled as strictly harder than latching — clearing the
reported upper edge is explicitly *not* enough — which is what makes the exit overshoot and
`tilt_zone_release_pct` load-bearing rather than decorative. Plans correct under this model are
correct under milder ones, since none of them relies on a crossing *not* latching. It records a
**violation** whenever it is commanded to travel below the lower edge while latched, and it can be
configured with the feedback quirks real controllers exhibit — duplicate settled reports, no motion
state, a final report only, a settled-looking echo of the position a move started from, and
calibration drift.

`test_model.py` drives the whole app against it and asserts, on every run: zero violations; a belief
never more confident than the truth; a position belief equal to what the actuator reports; a
published position equal to the spec mapping of it; completion **without the settle timer firing**;
and a bounded command count. It sweeps every intent from every whole position 0-100, from every
latched slat position, from every interrupted latch sequence and from the resting state after
leaving tilt; interrupts every multi-step sequence at every feedback point with every other intent,
a stop, and a restart with and without the cover going unavailable first; and repeats the intent
sweep under each feedback quirk. It then repeats the position, slat-position, quirk and
interrupted-entry sweeps on the geometries the two optional settings produce — a release height far
above the zone (so the ambiguity band is much wider than the zone) and an entry landing that is
neither zone edge — and asserts that each entry lands on the configured slat angle and each exit
physically clears the release height. The drift tests demonstrate why every latch sequence starts
from the top limit, pin the bound the cheap tilt exit depends on, and show the exit failing to
release when it is commanded at exactly its acceptance threshold.
