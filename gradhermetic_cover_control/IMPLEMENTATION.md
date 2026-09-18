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
| `config.py` | Parses `apps.yaml` args (all real travel percent); the numeric rules are delegated to `geometry` | pure |
| `runtime.py` | The settle-timer handle and the command rate limiter | state |

Each app instance manages exactly one blind.

The adapter's whole vocabulary is the `Action` list the core returns: `move_to` / `open_full` /
`close_full` / `stop` become real-cover service calls, `publish_state` becomes a `set_state`,
`arm_settle_timer` / `cancel_settle_timer` become `run_in` / `cancel_timer`, and `notify` becomes a
persistent notification. What is left in the adapter is transport only: listening and filtering,
gating commands until the startup state is seeded, decoding KNX telegrams (a direction for the move
and step addresses, a trigger-or-ignore for the tilt address) and button presses, the
command rate limit, and the callback `try`/`except` boundary.

## Transport: template cover, not MQTT

The virtual cover is surfaced to Home Assistant **without MQTT** — the config repo has no broker. A
small template cover (defined in the HA config) does two things and nothing else:

- Its `open_cover` / `close_cover` / `stop_cover` / `set_cover_position` actions fire a
  `gradhermetic_command` event that this app listens for.
- Its `position` template reads `sensor.gradhermetic_<virtual_id>_position`, which the app publishes
  itself via `set_state` after every event that changes what the cover shows — every position
  report during travel included. Its `state` template reads the sensor's `motion` attribute
  (`opening` / `closing` / `idle`) so the cover reports travel as it happens, and its `availability`
  template keeps an unpublished sensor from rendering as a closed blind. No `input_number` helper is
  involved — the app owns the sensor. Beside it the app publishes
  `binary_sensor.gradhermetic_<virtual_id>_tilt_mode`, `on` exactly while slat control is engaged;
  the template cover does not read it, but a dashboard does.

Step and tilt controls are exposed the same broker-free way: three name-only `input_button` helpers
(`..._step_up`, `..._step_down`, `..._tilt`) that carry no logic. The app listens for each press and
routes it into the logic engine (see "Virtual Cover Wiring").

All decision-making stays in `logic.py`; the template and helpers contain no logic. The
`set_cover_position` value, the open/close mapping, and latching are all decided by the Python app.

## Belief

The app persists nothing across restarts, so everything it does follows from three tracked facts:

- `last_position` — the blind's real travel position (0-100) from controller feedback, or `None`
  once the cover becomes unavailable. An unknown position makes every guard conservative
  automatically.
- `latch` — `LATCHED` / `UNLATCHED` / `UNKNOWN`. This is **event-sourced, not derived from the
  position**: a position inside the band is neither necessary nor sufficient for being latched, so a
  positional test cannot tell "known released" from "no idea", and that distinction is what decides
  whether a descent needs a release first.
- `is_moving` — whether the blind is travelling, from controller feedback — and, while it is, which
  way: the cover's `opening` / `closing` state when it reports one, else the trend of its positions.

The latch transitions, in full:

- → `LATCHED`: a completed enter sequence, and nothing else.
- → `UNLATCHED`: a completed plan that ends released, or feedback placing the blind clearly outside
  the `[lower - epsilon, release_target]` band, where a latched mechanism cannot rest.
- → `UNKNOWN`: startup with the position unknown or inside the band; a plan interrupted (stopped,
  replaced or stalled) part-way; externally-caused motion ending inside the band; the cover becoming
  unavailable; a completed height move that rose by position command to exactly `release_target`
  without provably staying above the lower edge (see invariant R1).

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
  tilt exit, which travels to the top limit but has done its job as soon as it is clear of the
  release target.

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
   was. (A slat step is only a percent or so of real travel, so this matters.) A settled report
   short of the target *after* the blind was seen moving is a blind that stopped somewhere else:
   the timer is re-armed for `SETTLED_RECHECK_SECONDS` instead of the full timeout, so a percent of
   mis-settling costs seconds rather than most of a minute.
4. **The settle timer fires.** Still moving → re-arm, because this is an inactivity timeout and not
   a travel-time cap. Settled and satisfied → complete, which covers an actuator that reported only
   its final state or none at all. Settled within `DEVIATION_TOLERANCE_PCT` of a `MoveTo` target →
   accept with a logged warning, because real actuators occasionally stop a percent off — but only
   for an ordinary height move whose target is clear of the band. A slat step is smaller than the
   tolerance (accepting would complete a move that never happened), and a target on a band edge or
   inside the band — the enter dip, the latching rise, the release height — is where a percent
   decides whether the mechanism latched or released. Those land exactly or stall. Otherwise → drop
   the plan and notify; the blind is at rest, so no stop is sent (see "Safety Behavior"). A position
   that has become unreadable is the one stall that does send a stop, since the blind may still be
   travelling.
5. **Completion.** The plan's terminal latch belief is committed and the settle timer is cancelled.
   The executor never publishes: `logic` does, after every event, from its own belief (see
   "Virtual Cover Wiring").

A command arriving while a plan is in flight **replaces** it. The replacement is planned from the
belief as it will be *after* the interruption, so it re-derives every safety guard; an intent that
plans to nothing (a slat step outside tilt, say) leaves the running plan alone.

## Canonical Sequences

- **Enter tilt** — `open_full`, then `MoveTo(lower - epsilon)`, then `MoveTo(upper)`, then
  optionally `MoveTo(virtual_to_real(landing))`. One sequence, correct from any start. The leading
  full open re-references the actuator at its limit switch (see the README on why the percentages
  are only reliable from there) and makes the dip a pure descent, which cannot latch.

  The latching rise can only end at the upper edge, so any other landing is one more in-zone slat
  move. That landing is unconditionally the configured `tilt_enter_landing_pct` — a real position
  inside the zone, which `Zone.enter_landing_virtual` converts to the virtual scale the intent
  carries — however the entry was triggered: the tilt helper, the KNX slat-mode address and the
  `set_tilt_mode` event all resolve to the same enter intent with the same configured landing. The
  fourth step is dropped when its landing rounds to the same integer command as the upper edge,
  because a command that repeats the current setpoint moves nothing.
- **Leave tilt** — `RiseToAtLeast(release_target)` carried by `open_full`, available only from a
  confident `LATCHED` belief. `release_target` is `tilt_zone_release_pct`, or `upper + epsilon` when
  that is not configured.

  This is the step that most needs its command distinct from its target, and now they are as far
  apart as they can be: it *travels* to the top limit and is *satisfied* at the release height.

  Going all the way up is both what the user means and what is safest. Leaving slat mode is a
  request to control the blind as a whole again, and stopping a few percent above the zone parks it
  in the ambiguity band with the slats still shut. It also removes the failure a short rise had:
  `epsilon` is sized to carry the *reported* position clear of the upper edge, which says nothing
  about how far the mechanism must travel to disengage, and an actuator settling a percent low can
  satisfy `>=` on a rise that physically fell short — leaving the app confidently, and wrongly,
  believing it is released. A limit switch cannot be settled short of.

  Accepting at `release_target` rather than at `100` is what keeps the step honest in the other
  direction: the mechanism has provably let go by that height, so a blind that comes to rest a
  percent below its own top limit still completes the plan without appeal to the executor's
  deviation tolerance, which does not forgive rise steps at all.
- **Guarded descent** (close, long-down, a descending `set_position`) — when `may_be_latched`,
  prefix `open_full`: an uncertain latch belief also means an uncertain calibration, so a short rise
  to a merely *reported* release height cannot be trusted. When the latch is known released,
  descend directly.
- **Normal-mode `set_position`** — snap the target clear of the band (to the nearer edge, or the
  far edge when the nearer one is where the blind already rests), then one `MoveTo`, guarded when it
  descends or the position is unknown.
- **Height step** — `position ± height_step_pct`, snapped clear of the band *in the direction of
  travel* (down continues to `band_low`, up to `band_high`), then the same single guarded `MoveTo`.
  A step that rounds to the current position (a travel limit) plans nothing.

  Both height moves commit `UNLATCHED` except in one case: a position-commanded rise that ends
  exactly on `release_target` and started below the lower edge, or from a belief that was not a
  known release. Such a rise may have latched on the way up and reached the release height with
  zero margin, and a position command cannot vouch for a release — so the plan commits `UNKNOWN`
  and the next descent buys the full open. Feedback clears it as soon as the blind rests above the
  band.
- **In-tilt moves** — a single `MoveTo` inside `[lower, upper]`.

```text
        enter: open fully, down to (lower - epsilon), up to upper, then to the landing
   NORMAL  ───────────────────────────────────────────────────────────►  TILT
 (height control)                                                  (slat control, latched)
      ▲            leave: open fully (satisfied once past release_target)     │
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
- **X1** — leaving tilt is a single upward step, only planned from a `LATCHED` belief; from an
  uncertain belief the release is L1's full open instead. It must accept no lower than
  `release_target`, and must be carried by the `open` *command*, since only a move referenced
  against the top limit switch is immune to the calibration error a release cannot afford.
- **R1** — a normal plan commits `UNLATCHED` only if no position-commanded rise in it ends on
  `release_target` having possibly crossed the lower edge while latched: the rise must start at or
  above the lower edge from a known release, or follow a full open. Otherwise the plan commits
  `UNKNOWN`. This closes the gap between X1's reasoning and the slider: a position-commanded rise to
  the release height is exactly the release X1 refuses to trust.

N1, T1 and L1 — and `can_change_latch` with them — check both the satisfaction target and the
commanded position of every step, since the hazard is where the blind physically travels and the two
are allowed to differ. Today only the tilt exit makes them differ, and it does so upward.

## Tilt-Zone Math

**Every configured percentage is real blind travel** — the numbers the actuator reports and accepts.
The virtual slat scale below is internal: it is what the cover entity's position slider shows while
tilt mode is engaged, and the scale the planner's intents are stated in. `geometry.Zone` is the only
place the two meet, and the two slat settings cross the boundary there —
`Zone.enter_landing_virtual` converts the configured `tilt_enter_landing_pct`, and `Zone.step`
converts `tilt_step_pct` (`tilt_step_pct / span * 100`).

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
  `tilt_enter_landing_pct` if that is not `44` as well (it is already a real position, so no
  conversion is involved in the move itself).
- leaving drives fully open, and is satisfied on the way once the blind reports `release_target` —
  `upper + epsilon = 46` unless `tilt_zone_release_pct` says otherwise.

Because the zone is narrow (6% here) and KNX actuators report integer positions, the zone holds only
about `span + 1` distinct slat positions (~7 for a 6% zone). A slat step must therefore be at least
one whole reported percent of real travel, otherwise the rounded position command repeats the
current position and the blind never moves. Config validation enforces `tilt_step_pct >= 1.0` so
every step advances the actuator, and `tilt_step_pct <= upper - lower` because a step wider than the
whole zone is not a step; `tilt_zone_epsilon_pct >= 1` likewise, so the dip and release targets round
to integers distinct from the edges they must clear. The band must also stop short of both travel
limits: `lower - epsilon > 0` and `release_target < 100`. A blind resting on either end stop is one
the app has to be able to trust as unlatched — that trust is what lets a restart at 0 or 100 resume
whole-height control without re-referencing — so a band that reached a limit would be rejected. The
two optional settings are validated here too: `tilt_zone_release_pct` must be at least
`upper + epsilon` (below the clearance it would not even carry the reported position out of the zone)
and below `100`, and `tilt_enter_landing_pct` must be a real position in `[lower, upper]`, since it
is a slat position. All of it lives in `geometry.Zone`,
which validates on construction — `config.py` only checks that each number is present (or, for the
optional two, absent), numeric and in range.

The ambiguity band runs `[lower - epsilon, release_target]`, and `band_high` is *defined* as
`release_target` rather than merely coinciding with it: a mechanism that is latched but has not yet
been released can physically be resting anywhere up to the height at which it lets go, so that is
exactly how far "latched cannot be ruled out" reaches. Everything derived from the band inherits a
configured release height automatically — `in_band`, `snap_normal_target`, the latch belief seeded at
startup, and the feedback rule that clears a latch belief.

Outside tilt, a `set_cover_position` target landing strictly inside the band `(36, 46)` here is
snapped to the nearer band edge, ties rising — unless that edge is where the blind already rests,
in which case the far edge is used, since a slider dragged into the band asked for a move. A height
step into the band snaps in its direction of travel instead (down to `36`, up to `46`), so a run of
step presses crosses the band in one press rather than stalling in front of it. Rising into the band
from below silently engages the latch, so a whole-blind move that aimed there would leave belief and
reality diverging; snapping costs a couple of percent of travel and makes "normal mode never targets
the band interior" an invariant (N1) instead of a hazard. Raising `tilt_zone_release_pct` raises
`band_high` with it, so the snap grows to cover every height at which the blind might still be
latched — that widening is the deliberate price of knowing when the mechanism has actually released.
It is the only cost of setting the value high, since the exit travels to the top limit either way.

## KNX Wall-Button Handling

Two dedicated group addresses drive the app as `knx_event`s (telegram value `0 = up / more light`,
`1 = down / less light`, matching the repo convention):

- **Move address** — long presses. Long up drives fully open (leaving tilt naturally); long down
  drives fully closed (driving fully open first unless the latch is known released, since the latch
  releases only upward).
- **Step address** — short presses, evaluated in priority order (`logic._step`, shared with the
  dashboard step helpers):
  1. If anything is moving — a plan in flight or the blind reported travelling — stop it.
  2. Otherwise, if latched, step the slats by `tilt_step_pct` of real travel (up toward open, down
     toward closed). An up step at the open edge leaves tilt upward and resumes whole-height
     control; that is the one thing the wall button does that the dashboard helpers do not.
  3. Otherwise step the height by `height_step_pct` (see "Canonical Sequences").

  A short press never enters tilt; the tilt address does.
- **Tilt address** — a stateless trigger (acts on `1`, ignores `0`) routed to
  `logic.on_toggle_tilt_mode`, the same toggle the dashboard tilt helper uses: leave when latched,
  enter otherwise, and cancel an entry or exit that is still in flight.

## Virtual Cover Wiring

Commands reach the app as a `gradhermetic_command` event carrying `virtual_id` and `command`
(`open` / `close` / `stop` / `set_position` with `position`, or `set_tilt_mode` with `enabled`). The
app filters by `virtual_id` and routes each to the logic engine. State is reflected back with
`set_state` on `sensor.gradhermetic_<virtual_id>_position`, which the template cover displays, and on
`binary_sensor.gradhermetic_<virtual_id>_tilt_mode`.

Both are written from one `PublishState` action, never separately. The position is on the inverted
virtual slat scale while latched and on the real height scale otherwise, so a reader that saw the two
disagree — even briefly — would be reading a slat angle as a height. Carrying them on one action
makes that impossible by construction. The flag is also the only honest source for the mode: a real
position inside the tilt zone is neither necessary nor sufficient for being latched, which is why the
app event-sources a latch belief in the first place, so nothing on the HA side can derive it.

The two `set_state` calls the action drives are not equally frequent, though: the position sensor is
written on every publish, since a publish means position or motion moved, while the tilt-mode sensor
is written only when `in_tilt` itself has changed — repeating the same `on` or `off` has nothing new
to report, and skipping it costs nothing, since the entity's last written value is still current.

The action carries a third value, `motion` (`opening` / `closing` / `idle`), published as an
attribute of the position sensor; the template cover's `state:` template turns it into the
`opening` / `closing` state a tile animates. It is stated on the *virtual* scale: while latched a
real descent opens the slats, so it is `opening`. The real direction comes from the cover's own
`opening` / `closing` state when it reports one, else from the trend of reported positions, else —
before any report on the outstanding command has arrived at all — from the step being driven toward,
on the theory that a command just went out and the blind is about to move that way. Once the
controller has reported the blind at rest, motion reads `idle` even while a plan is still pending: a
settled report short of a target means the blind has already stopped, not that it is still headed
there, and that holds through the settled-short recheck exactly as it does at genuine completion.

`logic._publish_current` runs after every event — feedback, timer, command, stop — and emits the
action only when one of the three values changed since the last publish, so a duplicate report costs
nothing and the sensor never goes stale after a stop or a stall. The mode it publishes is the belief
the app would hold if the plan in flight were interrupted at that instant (`_belief_after_interrupt`),
never the plan's hoped-for outcome: a slat move keeps slat mode, an entry shows height mode and the
real height climbing to the top and back until it actually latches, and an exit shows height mode
from its first command. An unreadable position is published as `position=None`, which the adapter
writes as `unavailable`.

`..._tilt` is an `input_button`: a press is a moment, not a state. A UI toggle that wants to show
which mode the blind is in therefore reads the binary sensor, not the button.

Step and tilt reach the app as `input_button` presses. The app watches
`input_button.gradhermetic_<virtual_id>_step_up` / `_step_down` and routes each to `on_step`
(up/down), and `..._tilt`, which it routes to `on_toggle_tilt_mode`. `on_step` is the KNX
stop/step rule minus the wall button's upward exit: stop if anything is moving, else a slat step
while latched (clamped at both edges), else a height step. A press that plans nothing — at a travel
limit, at a slat edge, with no position — is logged with the reason.

`on_toggle_tilt_mode` cancels an entry or exit that is still in flight rather than reading the
mode off a belief that is mid-transition; read that way, a toggle would restart the very sequence
the user is tapping at, one real-cover command per tap, straight into the rate limit. Otherwise it
toggles away from the mode the cover is *displaying* — `_displayed_in_tilt`, the belief
`_publish_current` shows — and not from the raw latch. The two disagree through any `PLAN_NORMAL`
started from a latched belief, which a KNX long press up begins: such a plan may release the latch,
so the displayed belief drops to height mode at its first command while the raw latch stays
`LATCHED` until the plan finishes. Toggling off the raw latch there would ask to leave a mode the
chip already shows as left, which plans nothing at all — a chip that visibly does nothing when
tapped.

`on_set_tilt_mode` decides "already in the requested mode" the same way, and treats a request for
the mode already being entered or left as a no-op. A request to leave during an entry, and a request
to enter during an exit, both stop the plan that is running: that is the nearest thing to what was
asked, and it leaves the blind where the user can see it rather than letting a sequence they just
contradicted run to completion behind them. Every one of these, like a step press that plans
nothing, is logged with its reason.

Tilt mode is also toggled from Home Assistant with a `gradhermetic_command` event carrying
`command: set_tilt_mode` and `enabled: true|false` — this is the HA-facing entry point. A call whose
`enabled` is missing is ignored (rather than silently coerced to "leave tilt").

## Restart Behavior

State is **not** persisted across restarts. `STARTUP_DELAY_SECONDS` after startup the adapter reads
the real cover's position and hands it to `logic.on_startup`, which seeds the belief — and emits no
movement at all:

- position clearly **outside** the band (beyond `lower - epsilon` … `release_target`): the blind
  cannot be latched, so the belief starts `UNLATCHED` and whole-height control resumes from that
  position.
- position **inside** the band, or unknown: the latch state is ambiguous, so the belief starts
  `UNKNOWN` and stays there.

An `UNKNOWN` latch belief is exactly what the planner's guards already key off, so re-referencing the
actuator happens **lazily**, in the first plan that needs a trusted position: `_guard_descent`
prefixes `open_full` to every descent while `may_be_latched`, and the enter sequence opens fully by
construction (which is also what the tilt control does from an unlatched belief). `open` and a long
up press re-reference by themselves, since they run the actuator to its limit switch. Startup
therefore buys nothing an action would not buy for itself — and the app never raises the blind
unprompted after a power cut.

Commands are ignored until the seed has run: a command arriving before it would act on an unseeded
belief, and every safety guard is derived from that belief.

Startup publishes what it seeded — `unavailable` if the real cover had no position yet — and every
reading after that publishes whatever changed, so a restart while Home Assistant is still booting the
real cover corrects itself on the first real reading, and a feedback-only latch change (a controller
that reports positions without ever reporting motion, say) updates the mode the moment the app stops
believing in it. AppDaemon re-initialises the app when Home Assistant restarts, which is what brings
the two app-owned entities back after HA has forgotten them.

## Safety Behavior

Each blind has a real-cover command rate limit. If the app sends more than `COMMAND_RATE_LIMIT`
commands within `COMMAND_RATE_WINDOW_SECONDS` (guarding against a plan whose waypoint is never
reached), the blind is disabled until AppDaemon restarts and a Home Assistant persistent notification
is created.

The `SETTLE_TIMEOUT_SECONDS` fallback timer only declares a stall — dropping the plan and raising an
obstruction notification — when the blind has **settled** short of its target. A move still reporting
motion when the timer fires is treated as merely long: the timer re-arms and waits, so a slow travel
never triggers a false stall. A pending plan whose position has become unreadable (the cover went
unavailable) is treated as a genuine stall rather than being left to hang silently. Healthy moves
never rely on the timer at all — the model tests assert every nominal flow completes without it
firing.

A stop command goes to the real cover only when the blind may actually be travelling: the controller
reports it moving, or a command has gone out and no settled report for it has come back yet. Once the
controller has reported the blind at rest, no stop is sent — not even while a plan is still pending,
which is exactly what keeps the `SETTLED_RECHECK_SECONDS` wait after a settled-short report from
sending one: the blind has already stopped by itself. A settled stall sends none either, and neither
does `cover.stop_cover` on a blind already at rest. Every command reaches the actuator through this
app, and on a KNX actuator with no dedicated stop object Home Assistant carries a stop on the step
object — which nudges an idle blind one notch instead of stopping it.

Reading a settled report as "stopped" is only sound on a controller that reports motion at all. Some
integrations publish positions and never an `opening` / `closing` state, and those report `is_moving`
false for the whole of a move, so the same inference would throw the stop away on a blind that is
still travelling. `logic` therefore remembers whether this controller has *ever* reported motion, and
until it has, an outstanding plan is the only signal there is and a stop is sent on it. Both covers
this repo drives do report the state, so the fallback is insurance rather than everyday behaviour —
but the app is integration-agnostic and the model tests exercise a controller that withholds it.

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
Three pieces live in the private HA config repo. The two entities the app owns
(`sensor.gradhermetic_<id>_position` and `binary_sensor.gradhermetic_<id>_tilt_mode`) need no helper
declared — the app publishes both.

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
  name: Living Room Blind Slat Mode
  icon: mdi:blinds-horizontal
```

The tilt helper's icon is fixed, like any button's. A dashboard control that should show the current
mode binds its icon, colour and label to `binary_sensor.gradhermetic_living_room_tilt_mode` and
presses this button on tap.

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
        # opening/closing from the motion attribute the app publishes; open/closed from the
        # position. Unavailable until the app has published, rather than a closed blind at 0%.
        state: >-
          {% set sensor = 'sensor.gradhermetic_living_room_position' %}
          {% set motion = state_attr(sensor, 'motion') %}
          {% if motion in ['opening', 'closing'] %}{{ motion }}
          {% elif states(sensor) | int(0) > 0 %}open
          {% else %}closed{% endif %}
        availability: "{{ has_value('sensor.gradhermetic_living_room_position') }}"
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

The move/step/tilt group addresses driving this app must be **input-only** — programmed in ETS so
they do not directly command the blind actuator. The app mediates every press and issues the
actuator's `position` commands itself; if the buttons also drove the actuator, tilt latching would
be bypassed.

Those addresses reach Home Assistant as `knx_event`s through the `event:` block `knx.yaml` includes
(`knx/knx_events.yaml`): one list entry whose `address:` names every group address that should fire
one — long press, short press and slat-mode toggle, for every blind wired up, three per blind:

```yaml
# knx/knx_events.yaml
- address:
    - "2/6/7"   # Playroom Shutter - long press
    - "2/7/7"   # Playroom Shutter - short press
    - "2/8/7"   # Playroom Shutter - toggle slat mode
    - "2/6/21"  # Jocelyn's Office Shutter - long press
    - "2/7/21"  # Jocelyn's Office Shutter - short press
    - "2/8/21"  # Jocelyn's Office Shutter - toggle slat mode
```

No `type:` is declared. A KNX DPT type exists to decode the raw bit into a friendlier value, but
every one of these addresses carries a plain one-bit telegram and the adapter decodes it itself:
`_knx_direction` reads a `0`/`1` as up/down for the long- and short-press addresses, and
`_knx_trigger` reads a lone `1` as "act" and a `0` as "ignore" for the slat-mode address. Declaring a
type here would only make KNX decode the bit into a form the adapter would have to undo.

### 4. KNX status expose (`knx_expose.yaml`)

Everything above only goes one way: the bus drives the app, and nothing on it can see what the app
believes. `knx/knx_expose.yaml`, included as the `expose:` block in `knx.yaml`, mirrors both of the
app's own entities back onto their own group addresses, the same pattern the Room 1 moon light uses
for its KNX presence — the bus drives Home Assistant on one address, Home Assistant reports back on
another:

```yaml
# knx/knx_expose.yaml
- entity_id: "binary_sensor.gradhermetic_playroom_shutter_tilt_mode"
  address: "9/3/0"
  type: "binary"
- entity_id: "sensor.gradhermetic_playroom_shutter_position"
  address: "9/3/1"
  type: "percent"
```

Both entities are exposed, never only one, because neither means anything alone: the position is the
app's *virtual* one, a slat angle while `..._tilt_mode` reads `on` and a real height otherwise (see
"Virtual Cover Wiring"), so a bus-side reader — a panel, a scene, a logic block elsewhere on KNX —
has to read the tilt-mode bit before the position beside it means anything, exactly as a dashboard
does. Nothing writes to these addresses from the bus, so there is no way back in through them;
`knx/knx_events.yaml` is the only inbound path.

KNX's expose only has something to send when the entity it watches holds a value. While the app
publishes the position sensor as `unavailable` — before the first startup reading, or whenever the
real cover itself goes unavailable — expose sends nothing at all, so the bus simply keeps holding
whatever position it last received rather than being told anything false.

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

  # Every percentage here is real blind travel, never the inverted virtual slat scale: tilt_step_pct
  # is the real travel one slat step moves the blind (>= 1.0, <= the zone's width).
  tilt_zone_upper_pct: 44.0
  tilt_zone_lower_pct: 38.0
  tilt_zone_epsilon_pct: 2.0
  tilt_step_pct: 1.2

  # Optional; one step button press outside slat mode moves the blind this much. Defaults to 2.0.
  height_step_pct: 2.0

  # Optional; see the README for how to measure the release height and pick a landing. The landing
  # is an absolute position inside the zone (44 = slats closed, 38 = fully open).
  tilt_zone_release_pct: 50.0
  tilt_enter_landing_pct: 42.8

  knx_move_address: "2/6/0"
  knx_step_address: "2/6/1"
  knx_tilt_address: "2/6/2"
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
| `test_executor.py` | Skip-if-satisfied, duplicate-feedback immunity, every settle-timer path (re-arm, accept, deviation and where it is refused, stall with and without a stop, unreadable), the settled recheck, cancel-on-completion |
| `test_logic.py` | The event surface end to end, belief transitions, plan replacement, the shared step rule in both modes, what is published when (progress, mode, motion, unavailable), the tilt toggle, and named regressions for the four bugs the redesign removed |
| `test_config.py`, `test_runtime.py` | `apps.yaml` parsing and the command rate limiter |
| `test_adapter.py` | The AppDaemon layer against a fake `hass.Hass`: wiring, event filtering, the startup gate, malformed payloads, button edge detection, every `Action`'s translation, the rate limit and the error boundary |
| `simulator.py`, `test_model.py` | See below |

`simulator.py` is an independent ground-truth model of the mechanism, under the most pessimistic
latch semantics consistent with the hardware: any upward crossing of the lower edge from below
engages the latch, only a rise that actually reaches `release_target` releases it, and downward
travel never changes it. Releasing is modelled as strictly harder than latching — clearing the
reported upper edge is explicitly *not* enough — which is what makes `tilt_zone_release_pct`
load-bearing rather than decorative. Plans correct under this model are
correct under milder ones, since none of them relies on a crossing *not* latching. It records a
**violation** whenever it is commanded to travel below the lower edge while latched, and it can be
configured with the feedback quirks real controllers exhibit — duplicate settled reports, no motion
state, a final report only, a settled-looking echo of the position a move started from, and
calibration drift.

`test_model.py` drives the whole app against it and asserts, on every run: zero violations; a belief
never more confident than the truth; a position belief equal to what the actuator reports; a
published position equal to the spec mapping of it; completion **without the settle timer firing**;
and a bounded command count — and, at every publish rather than only at rest, that slat mode is
never shown while the mechanism is not latched. It sweeps every intent from every whole position
0-100, from every latched slat position, from every interrupted latch sequence and from the resting
state after leaving tilt; walks the blind from top to bottom and back with step presses alone;
interrupts every multi-step sequence at every feedback point with every other intent, a stop, and a
restart with and without the cover going unavailable first; and repeats the intent sweep under each
feedback quirk. It then repeats the position, slat-position, quirk and
interrupted-entry sweeps on the geometries the two optional settings produce — a release height far
above the zone (so the ambiguity band is much wider than the zone) and an entry landing that is
neither zone edge — and asserts that each entry lands on the configured slat angle and each exit
both physically clears the release height and finishes at the top limit. The drift tests demonstrate
why every latch sequence starts from the top limit, and show a *position*-commanded release failing
on a drifted actuator — the failure the exit avoids by driving against the limit switch instead, and
the one R1 keeps a slider target from walking into.

The bound they pin is tighter than `tilt_zone_epsilon_pct` on its own suggests. Calibration error
only clears at a travel limit, and the enter sequence spends up to three position commands — the
dip, the latching rise, the configured landing — before any slat move begins, none of which touches
a limit, so whatever error the actuator adds per move gets three chances to compound first: the
tolerable per-move error is roughly a third of the margin, not all of it.

A slat move to the fully-open edge is where that shows up first, because it targets `lower` exactly
rather than a clearance-padded number — the one landmark in the design with no margin of its own.
That is deliberate, and `TestTheSlatOpenEdge` in `test_model.py` carries the argument. The rule a
margin here would pad is L1, "never travel below the lower edge while latched", and what L1 exists
to prevent is a plan *aiming* a descent through the slat range on an engaged mechanism. No plan
ever does; the planner sweep proves it, and the test restates it directly as "no in-zone intent,
from any slat position, on any configured geometry, ever commands or targets below `lower`".

The only thing a margin would add is padding against the actuator overshooting its own setpoint,
and the only way to buy it is to raise `tilt_zone_lower_pct`, which moves what virtual `100`
*means* — fully open would stop short of the slats being fully open. That is a bad trade: the cost
is visible on every slat move, while the overshoot is a few tens of milliseconds of travel against
a stop the slats have already reached. An actuator drifting that far has already displaced every
slat angle and the entry landing with them, so the open edge is not a weak point in the design,
just the place a zero-tolerance assertion notices drift first. The bound above is the honest
statement of how much drift the geometry tolerates; the answer to exceeding it is a re-measured
zone, not a padded edge.
