# Gradhermetic Cover Control — Redesign Plan

This document is the work order for reimplementing `gradhermetic_cover_control` in a cleaner and
more provably correct fashion. The user-facing contract in [README.md](README.md) remains the
authoritative specification, **as amended by the decisions in section 2**. The implementing agent
should read README.md first, then this document, then the current sources (they encode many correct
details worth preserving even where the structure changes).

The existing test suite (66 tests) passes and encodes valid scenarios — port them, don't discard
them.

---

## 1. Review findings on the current implementation

### 1.1 Confirmed bugs (reproduced against `logic.py` as of this writing)

**B1 — 45-second stall on no-op waypoints.** A movement plan only advances when position feedback
arrives, but a waypoint that equals the blind's current position produces no actuator motion and
therefore no feedback. The plan hangs until the `SETTLE_TIMEOUT_SECONDS` (45 s) fallback timer
fires. Concrete repro: leave tilt mode (blind now rests at `upper + epsilon` = 46), then send
`cover.close_cover`. Because 46 is *inside* the inclusive ambiguity band `[36, 46]`,
`_guard_descent` prepends `move_to 46` — a no-op — and the blind does not start closing for 45
seconds. The same class of stall hits any plan whose first waypoint is already satisfied
(e.g. `open` in tilt when slats are already open).

**B2 — stale feedback completes a slat step before the blind moves.** Waypoint arrival is
`|position − target| ≤ POSITION_TOLERANCE_PCT` (1.5). A slat step of `tilt_step_pct = 20` virtual %
in a 6 % zone is only **1.2 real %** — less than the tolerance. Any settled feedback event carrying
the *old* position (the state listener uses `attribute="all"` and fires on every attribute change,
so duplicates occur) instantly "completes" the plan and publishes the new virtual position while the
blind has not moved. Repro: latched at real 44 (virtual 0), `on_slat_step("up")` targets real 42.8;
feeding `on_real_position(44.0, False)` completes the plan and publishes virtual 20. The config
validation (`tilt_step_pct ≥ 100 / span`, i.e. ≥ 1 real %) is inconsistent with the 1.5 % tolerance
— steps are allowed that the arrival check cannot distinguish from "didn't move".

**B3 — stale motion/position belief when the cover becomes unavailable.** `_on_real_state` returns
early when the extracted position is `None`, so `logic._is_moving` and `_last_position` keep their
last values indefinitely. A KNX short press then executes "stop" (priority 1) against a blind that
is not moving, and descent guards reason from a position that may no longer be true.

**B4 — settle timer is never cancelled on normal plan completion.** It is cancelled only on
STOP/disable/error, so after every completed plan a stray `_on_settle` fires up to 45 s later.
Currently a harmless no-op, but it is exactly the kind of loose end that makes the timing behavior
hard to reason about.

### 1.2 Specification gap, resolved by a hardware fact from the user

README's enter-from-below rule said: from clearly below the band, first hop up to
`lower + epsilon` — a point **inside the tilt zone** — then dip and rise. But README also states
that the latch physically engages "the moment the rise crosses the lower edge" — which the hop
itself does, so the subsequent dip to `lower − epsilon` would drive the blind downward while
latched, precisely what the spec's own safety invariant forbids. The two statements could not both
be literally true.

The user has since supplied the governing hardware fact: **the enter-tilt sequence's percentages
are only reliable when the sequence starts from the fully open position (100 %)** — the actuator's
reported percentage cannot be trusted to match the blind's true physical position unless the move
is referenced from the top. This resolves the question: entry never hops from below and never dips
from an arbitrary height; it always drives fully open first (section 4.3). README must be amended
accordingly (phase 7).

### 1.3 Structural weaknesses driving the rewrite

- **The plan-advancement rule is the root of B1/B2.** "Position within tolerance and settled" is
  the *only* trigger, so it cannot distinguish "hasn't started moving yet" from "already there" from
  "arrived". Every race and stall traces back to this.
- **Stall/re-arm timing logic lives in the adapter** (`_on_settle`, `_restart_settle`,
  `_MOVE_SERVICES` coupling) where it is completely untested; the pure core cannot be exercised
  against timer events at all.
- **`_enter_tilt` is a four-way positional branch** (unknown / in-band / below / above) whose cases
  each encode part of the safety argument. The safety property is spread across `_guard_descent`,
  `_might_be_latched`, `_enter_tilt`, and `on_set_position`'s `descending` special case.
- **Duplicated validation**: `config.parse_app_config` re-implements every check in
  `LogicConfig.validate`, and imports `POSITION_TOLERANCE_PCT` from `logic` to do it.
- **Dead code**: `ACTION_PERSIST_STATE` is a no-op in the adapter; `seed_state`'s `in_tilt`
  parameter is only ever passed `False`; the position-`None` fallback in `_step_slats` is
  unreachable; the six listener handles stored in `CoverRuntime` are never used.
- **Undefined behavior left implicit**: normal-mode `set_cover_position` targeting inside the
  ambiguity band (see Q2); a short press while idle *inside* the band but not latched (falls through
  every branch of `_enter_toward_zone` and silently does nothing); a new command replacing an
  in-flight plan (works, and is guarded, but is nowhere stated or directly tested as a semantics).

---

## 2. Decisions (defaults chosen; user may override before implementation)

**Q1 — Enter-tilt reference point (RESOLVED by the user).** The latch-sequence percentages are
only reliable when starting from fully open. Consequences:
- Every enter-tilt plan begins by driving fully open via the open **command** (re-referencing the
  actuator at its top limit) regardless of the current position, then dips to `lower − eps`, then
  rises to `upper`. **One canonical sequence from any start** (section 4.3); the from-below "hop"
  and the from-above direct dip are both removed from README (phase 7).
- The same distrust of unreferenced percentages applies to the latch-release guard: when the latch
  state is genuinely **uncertain** (interrupted sequence, restart, external move — the `UNKNOWN`
  belief of section 4.1), releasing by rising to a *reported* `upper + eps` cannot be trusted
  either, so the guard releases by driving **fully open** before descending. When the latch state
  is confidently known (`LATCHED` freshly established by a calibrated entry, with only small
  in-zone moves since), README's cheap exit stands: leaving tilt rises to `upper + eps`.
- For the test simulator, keep the conservative latch model (*any* upward crossing of the lower
  edge that starts below it engages the latch; release only by rising across the upper edge), plus
  an optional calibration-drift knob (section 5.2). Plans correct under this model are also correct
  under milder reversal-only models — they never rely on a crossing *not* latching.

**Q2 — Normal-mode targets inside the ambiguity band.** `set_cover_position` (and KNX-derived
absolute moves) in normal mode **snap targets inside the open band `(lower − eps, upper + eps)` to
the nearest band edge** (`lower − eps` or `upper + eps`). Rationale: under the conservative model,
rising from below to a target inside the zone silently latches the mechanism while the app believes
it is doing height control — belief and reality diverge. Snapping (a few percent on a blind,
invisible in practice) makes "normal mode never targets the band interior" an invariant instead of
a hazard. The published virtual position is the snapped value.

**Q3 — Short press while idle with position inside the band (not believed latched).** Keep the
current behavior: no-op. Long press remains the escape hatch. Document it in README.

**Q4 — Command arriving while a plan is executing.** Keep the current behavior — the new command
**replaces** the plan, re-planned from the current belief (which re-derives all safety guards).
Make this explicit in IMPLEMENTATION.md and cover it directly with interruption tests (section 5.3).

---

## 3. Target architecture

Same package, same AppDaemon entry point (`module:
public_apps.gradhermetic_cover_control.gradhermetic_cover_control`, `class:
GradhermeticCoverControl`), same config schema, same HA wiring. Internals split by responsibility:

| Module | Responsibility | Purity |
|---|---|---|
| `geometry.py` | `Zone` dataclass: virtual↔real mapping, band predicates, named targets (`dip_target`, `release_target`), snapping (Q2), all validation of zone/step numbers (single source; `config.py` delegates here) | pure functions |
| `planner.py` | `plan(belief, intent) -> Plan` — every sequence and every safety guard lives here and only here; plus `check_plan(belief, plan)` asserting the invariants of section 4.5 | pure |
| `executor.py` | Drives one `Plan` step-by-step: step lifecycle, skip-if-satisfied, arrival detection, settle-timer decisions, stall detection. Consumes feedback/timer events, emits `Action`s | pure |
| `logic.py` | Thin façade `GradhermeticCoverLogic` holding the belief (`last_position`, `in_tilt`, `is_moving`) and composing planner + executor; keeps today's public `on_*` API so the adapter barely changes | pure |
| `gradhermetic_cover_control.py` | AppDaemon adapter: listeners, service calls, `set_state`, notifications, timers-as-mechanism. **No decisions** — it translates `Action`s one-to-one | I/O |
| `config.py` | Parse `apps.yaml` args into `GradhermeticConfig`; numeric validation delegated to `geometry` | pure |
| `runtime.py` | Rate limiter (keep as is) + the handles actually needed | state |

**The Action vocabulary grows so that *all* timing decisions move into the pure core:**

- `move_to(position)` / `open_full` / `close_full` / `stop` — as today.
- `publish_position(virtual)` — as today. (`persist_state` is deleted.)
- `arm_settle_timer(seconds)` / `cancel_settle_timer` — the adapter mechanically arms/cancels one
  timer and calls `logic.on_settle_timer()` when it fires. All re-arm / stall / accept decisions
  are made in `executor.py` and are therefore unit- and simulation-testable.
- `notify(kind, message)` — stall and error notifications become data; the adapter renders them as
  `persistent_notification/create`.

The adapter keeps only: entity/event listening and filtering, `_ready` gating during startup
recovery, `_is_button_press` edge detection, KNX telegram decoding, the rate limiter, the
try/except-disable boundary, and mechanical `Action` translation.

---

## 4. Core semantics, stated precisely

### 4.1 Belief state

- `position: Optional[float]` — last reported real travel position; set to `None` when the cover
  becomes unavailable/unknown (fixes B3; unknown position automatically makes every guard
  conservative).
- `latch: {LATCHED, UNLATCHED, UNKNOWN}` — event-sourced, not derived from position alone
  (position drift means the band test is neither sufficient nor necessary; see Q1). Transitions:
  - → `LATCHED`: **only** when an enter-tilt plan completes (E1).
  - → `UNLATCHED`: when a plan completes whose final state is not-latched (leave, guarded descent,
    recovery, any normal-mode plan), **or** when feedback places the blind clearly outside the
    band `[lower − eps, upper + eps]` (a latched mechanism cannot sit outside the zone).
  - → `UNKNOWN`: at startup with position unknown or inside the band; when a plan is interrupted
    (stopped, replaced, or stalled) while the position is inside the band or unknown; when
    feedback shows externally-caused motion ending inside the band with no plan in flight.
- `is_moving: bool` — from feedback; reset to `False` when position becomes unknown (B3).
- Derived helpers, the only forms guards may use: `in_tilt = (latch == LATCHED)` selects slat
  control; `may_be_latched = (latch != UNLATCHED)` triggers the descent guard.

### 4.2 Step model (fixes B1 and B2 by construction)

A plan is an ordered list of steps. Two step kinds, each with an explicit *satisfaction predicate*
in the **integer domain** the actuator actually speaks (commands are `round(target)`; KNX actuators
report their own setpoint-reached encoder value, so exact integer comparison is the honest check):

- `MoveTo(target)` — satisfied when `round(position) == round(target)`. `open_full`/`close_full`
  are `MoveTo(100)`/`MoveTo(0)` variants that map to `cover/open_cover`/`cover/close_cover` in the
  adapter (recovery must keep sending the open *command*, not a position — README requirement).
- `RiseToAtLeast(target)` — satisfied when `round(position) >= round(target)`. Used for the tilt
  exit from a confident `LATCHED` state (all other latch releases are full opens, per Q1).

Step lifecycle in the executor:

1. **Activation**: if the step's predicate already holds for the current position, the step is
   **skipped** — no command sent, next step activates immediately. This eliminates every no-op
   waypoint (the B1 stall class: a plan can no longer begin with a move the actuator will never
   acknowledge).
2. Otherwise the command is sent, the send-time position is recorded, and `arm_settle_timer` is
   emitted.
3. **Arrival**: on feedback, the step completes only when the blind is settled (not
   opening/closing) **and** the satisfaction predicate holds. Because activation guaranteed the
   predicate did *not* hold at send time, a duplicate feedback event at the send-time position can
   never satisfy it — B2 is structurally impossible, with no tolerance-vs-step-size tension left.
4. **Settle timer fires** (`on_settle_timer`): still moving → re-arm (a long travel is not a
   stall); settled and predicate holds → complete (covers an actuator that reported no intermediate
   states); settled within `DEVIATION_TOLERANCE_PCT` (new constant, ~2) of a `MoveTo` target →
   accept with a logged warning (real actuators occasionally stop 1 % off); otherwise → emit
   `stop`, clear the plan, emit `notify(stall, …)`. Position unreadable with a plan pending →
   stall, as today.
5. **Plan completion**: commit `final_in_tilt` and final virtual position, emit
   `publish_position` and `cancel_settle_timer` (fixes B4).

`POSITION_TOLERANCE_PCT` disappears as an arrival criterion. The `epsilon >
POSITION_TOLERANCE_PCT` validation is replaced by `epsilon >= 1` (targets must be distinct
integers from the zone edges); keep the `tilt_step_pct >= 100 / span` rule (each step must change
the commanded integer).

### 4.3 Canonical sequences (planner)

With the Q1 resolution, the four-way `_enter_tilt` branch collapses into **one** sequence, correct
from any start:

- **Enter tilt** (land at closed edge; append `MoveTo(lower)` to land open instead):
  1. `MoveTo(100)` via the open command — always first, from any position, re-referencing the
     actuator at its top limit (Q1). Skipped only when already reporting fully open.
  2. `MoveTo(lower − eps)` — the dip (pure descent from fully open: cannot latch).
  3. `MoveTo(upper)` — the rise across the lower edge latches; slats end parallel/closed.
- **Leave tilt** (from `LATCHED` only): `RiseToAtLeast(upper + eps)` — the short exit is trusted
  because the entry sequence freshly re-referenced the actuator and only small in-zone moves have
  happened since; final virtual = real position after the move.
- **Guarded descent** (close / long-down / normal-mode descending `set_position`): when
  `may_be_latched` (i.e. `latch != UNLATCHED`), prefix `MoveTo(100)` via the open command — a full
  re-referencing release, since a short rise to a *reported* `upper + eps` cannot be trusted from
  an uncalibrated state (Q1) — then the descent step. When `UNLATCHED`, descend directly: in
  particular, closing right after leaving tilt (resting at `upper + eps`, `UNLATCHED`) descends
  immediately, so the B1 stall cannot recur and no full-open detour is paid in the common case.
- **Normal-mode `set_position`**: snap the target per Q2, then a single `MoveTo`, with the descent
  guard when the (snapped) target is below the current position or the position is unknown.
- **In-tilt moves** (open/close/set_position/slat steps): single `MoveTo` inside `[lower, upper]`,
  exactly today's virtual↔real inversion; the KNX up-step at the open edge still escalates to
  leave-tilt; dedicated step helpers still clamp (README rules unchanged).
- **Recovery**: unchanged from README — outside the band: resume with belief `UNLATCHED`;
  inside/unknown: open command, then believe position 100, `UNLATCHED`.
- **KNX short/long priority rules**: unchanged from README.

### 4.4 Belief hygiene

- The `latch` transitions are exactly those of section 4.1 — feedback can only ever *degrade* the
  belief (`→ UNLATCHED` outside the band, `→ UNKNOWN` on external motion into the band); only a
  completed enter plan establishes `LATCHED`.
- Unavailable cover: position → `None`, `is_moving` → `False`; a pending plan stalls via the timer
  path (as today), and every later guard is automatically conservative (`UNKNOWN`).

### 4.5 Invariants (enforced by `planner.check_plan` at runtime and asserted by every test)

- **N1**: in normal mode, no `MoveTo` target lies strictly inside `(lower − eps, upper + eps)`.
- **T1**: slat `MoveTo` targets lie within `[lower, upper]`, and are planned only while the latch
  belief is `LATCHED`.
- **L1**: any step targeting below `lower` is preceded in the same plan by a full-open step
  **unless** the latch belief at plan time is `UNLATCHED`.
- **E1**: the latch belief becomes `LATCHED` only via the canonical enter sequence (fully open,
  dip below `lower`, rise to `upper`).
- **X1/R1**: leaving tilt and recovering are upward-only; every latch release from an `UNKNOWN`
  state is a full open (never a short rise to an unreferenced percentage).

`check_plan` runs on every plan before execution; a violation disables the blind and notifies
(defense in depth — it should be unreachable, and the exhaustive tests prove it is).

---

## 5. Test strategy — this is the "provably correct" part

Keep `unittest`, no new dependencies, same discovery command:
`python3 -m unittest discover -s gradhermetic_cover_control/tests -t .` from `apps/public_apps`.

### 5.1 Unit tests

- `geometry`: mapping round-trips, band predicates at exact edges, snapping, validation.
- `planner`: golden sequences for every intent × representative starts (above / below / in-band /
  at-band-edge / unknown), plus `check_plan` rejection cases.
- `executor`: skip-if-satisfied, duplicate-feedback immunity, settle-timer re-arm / accept /
  deviation / stall paths, cancel-on-completion, unavailable-mid-plan.
- Port the existing 66 tests' scenarios onto the new API (most exercise the façade and carry over
  nearly verbatim; expectations change only where this plan changes behavior — e.g. the
  from-below entry now rises above the zone first).

### 5.2 Mechanical simulator (`tests/simulator.py`)

A small ground-truth model of the physical blind under the Q1 conservative semantics:

- State: real position (float), `latched` (bool), motion in progress.
- Motion is simulated as a sequence of integer position reports; crossing the lower edge upward
  from below sets `latched`; crossing the upper edge upward clears it; while latched, travel within
  `[lower, upper]` is slat rotation (height notionally pinned at the latch point).
- **Violation detection**: the simulator records a violation if it is ever commanded to travel
  below `lower` while `latched`. Tests assert zero violations — this is the safety proof.
- Configurable feedback quirks, each exercised by the harness: duplicate settled events after
  arrival; motion state never reported (position jumps only); no event at all for a command that
  matches the current position; reports arriving only after travel completes.
- Optional **calibration drift** knob (Q1): a small offset between reported and physical position
  that accumulates on travel and resets to zero when the blind reaches its top limit. With drift
  enabled, the harness demonstrates why the from-100 rule exists: entry and release sequences must
  still latch/release correctly because every one of them starts from the re-referencing full
  open, while a hypothetical short-rise release from an uncalibrated state would fail.

### 5.3 Exhaustive model-based harness (`tests/test_model.py`)

Positions are integers 0–100, so the state space is small enough to enumerate rather than sample:

- **Single intents**: every start position 0–100 × every physically consistent latch state × every
  intent (open, close, stop, set_position over a representative target grid, set_tilt_mode on/off,
  slat steps, KNX short/long × direction) → run to completion against the simulator. Assert: zero
  simulator violations; the app's final belief (`in_tilt`, position) equals simulator truth; the
  published virtual position matches the spec mapping; the plan completes **without the settle
  timer firing** (nominal flows must never depend on the 45 s fallback — this is the regression
  gate for B1); bounded command count per intent (catches loops; complements the runtime rate
  limiter).
- **Interruptions**: for every multi-step sequence (enter, guarded descent), inject at *every*
  intermediate feedback point: a stop; each other intent (Q4 replace semantics); a simulated
  restart followed by recovery. Assert the same properties afterward.
- **Fault injection**: the feedback quirks of 5.2, plus settle-timer firings at legal points.
  Assert stalls are declared only when the simulated blind genuinely stopped short, and duplicates
  never advance a plan (regression gate for B2).

### 5.4 Named regression tests for the confirmed bugs

- `test_close_after_leaving_tilt_starts_immediately` (B1): after a completed leave-tilt (resting
  at `upper + eps`, belief `UNLATCHED`), `on_close`'s first emitted command is the descent itself —
  no no-op waypoint, no full-open detour, no timer dependency.
- `test_slat_step_ignores_stale_feedback` (B2): duplicate settled feedback at the pre-step
  position neither completes the plan nor publishes a position.
- `test_unavailable_clears_motion_belief` (B3) and `test_settle_timer_cancelled_on_completion`
  (B4).

### 5.5 Adapter tests

A minimal fake `hass.Hass` (stub `listen_state` / `listen_event` / `call_service` / `set_state` /
`run_in` / `cancel_timer` / `register_service` / `get_state` / `datetime` / `log`) verifying:
event filtering by `virtual_id`, `_ready` gating, malformed-payload tolerance (bad `set_position`
value, missing `enabled`, malformed KNX telegram), button edge detection, Action→service-call
translation including the timer and notify actions, rate-limit disable, and error-boundary
disable+notify. This layer currently has zero tests; it must not stay that way.

---

## 6. Implementation phases (each lands with its tests green; commit directly to `main` per repo convention)

1. **`geometry.py` + config dedupe** — extract mapping/validation; `config.py` delegates;
   existing config tests keep passing.
2. **`planner.py` + `check_plan`** — canonical sequences of 4.3, invariants of 4.5, golden-sequence
   unit tests.
3. **`executor.py`** — step lifecycle of 4.2 with timer actions; unit tests including all timer
   paths.
4. **Rewire `logic.py` as the façade** — same public `on_*` surface; port the existing 66 tests;
   add the named regression tests of 5.4.
5. **Simulator + exhaustive harness** (5.2, 5.3) — the correctness gate; fix whatever it finds.
6. **Adapter rewrite** — mechanical Action translation, fake-hass tests (5.5); delete dead code
   (`ACTION_PERSIST_STATE`, unused handles, `seed_state`'s `in_tilt` parameter).
7. **Docs** — update README.md (Q1's entry-sequence change, Q2 snapping, Q3 documented no-op) and
   IMPLEMENTATION.md (new module map, step model, timer semantics, Q4 replace semantics, test
   layout). Full suite run.

## 7. Compatibility constraints — do not change

- `apps.yaml` config schema and the module/class path.
- Event name `gradhermetic_command` and its payload shape; the AppDaemon service registration.
- Entity ids: `sensor.gradhermetic_<id>_position`, the three `input_button` helpers,
  `cover.gradhermetic_<id>`; the template-cover wiring in the private repo.
- KNX telegram conventions (0 = up / more light, 1 = down / less light) and address semantics.
- Upward-only recovery via the open *command*; no state persistence across restarts.
- Rate limiter behavior and the callback try/except-disable boundary.
