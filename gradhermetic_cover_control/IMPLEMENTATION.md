# Gradhermetic Cover Control Implementation

User-facing behaviour and configuration are in [README.md](README.md).

## Architecture

A pure core with a thin AppDaemon adapter. Every decision is made in code with no I/O, so it is all
testable without AppDaemon. Each app instance manages one blind.

| Module | Responsibility |
|---|---|
| `geometry.py` | `Zone`: config validation, band/zone predicates, named targets, band snapping, the real↔virtual mapping |
| `planner.py` | `plan(zone, belief, intent) -> Plan`: all movement sequences and latch guards; `check_plan` invariants |
| `executor.py` | Drives one `Plan`: step activation and arrival, settle timer, stall detection |
| `logic.py` | `GradhermeticCoverLogic`: holds the belief, routes events to planner/executor, publishes state |
| `gradhermetic_cover_control.py` | AppDaemon adapter (I/O only) |
| `config.py` | Parses `apps.yaml` args |
| `runtime.py` | Settle-timer handle and command rate limiter |

The core returns `Action`s, and the adapter translates them one-to-one:

| Action | Adapter call |
|---|---|
| `move_to` / `open_full` / `close_full` / `stop` | `cover.*` service on the real cover |
| `publish_state` | `set_state` on the two published entities |
| `arm_settle_timer` / `cancel_settle_timer` | `run_in` / `cancel_timer` |
| `notify` | persistent notification |

The adapter also filters events, decodes KNX telegrams and button presses, ignores commands until
the startup state is seeded, applies the rate limit, and wraps callbacks in the error boundary.

## Belief

- `position`: last reported real position, or `None` while unavailable.
- `latch`: `LATCHED` / `UNLATCHED` / `UNKNOWN`. Tracked from events, not derived from position:
  a position in the band neither proves nor rules out a latch.
- `is_moving` and direction: from the cover's `opening` / `closing` state, else the position trend.

Latch transitions:

- → `LATCHED`: a completed enter plan, nothing else.
- → `UNLATCHED`: a completed plan ending released, or feedback resting outside the band
  `[lower - epsilon, release]`.
- → `UNKNOWN`: startup in the band or with no position; a plan interrupted (stopped, replaced,
  stalled) part-way; external motion ending in the band; the cover going unavailable; a
  position-commanded rise ending on `release` that may have crossed the lower edge (R1).

Interrupting a pure slat plan (every target and command inside `[lower, upper]`) keeps `LATCHED`,
since it can't cross an edge (`planner.can_change_latch`).

The guards use only `in_tilt` (`latch == LATCHED`) and `may_be_latched` (`latch != UNLATCHED`).

## Plans and Steps

A plan is a sequence of steps plus the latch belief to commit on completion. Each step has a
satisfaction `target` and optionally a different `command_pct`:

- `MoveTo(target)`: satisfied when `position == target`. Positions are whole percents, and KNX
  reports the setpoint it reached. The `open_full` / `close_full` variants send `open_cover` /
  `close_cover` so the actuator runs against its limit switch.
- `RiseToAtLeast(target)`: satisfied when `position >= target`. Only the tilt exit uses it: it
  commands 100 and is satisfied at `release`.

Step lifecycle (`executor.py`):

1. **Activation.** A step already satisfied by a settled blind is skipped without a command. A blind
   still travelling is passing through, not resting, so nothing is skipped.
2. Otherwise send the command and arm the settle timer (`SETTLE_TIMEOUT_SECONDS`).
3. **Arrival.** Only settled feedback satisfying the step completes it. Since the step wasn't
   satisfied when commanded, a stale report of the starting position can't complete it. Settling
   short after being seen moving re-arms the timer for `SETTLED_RECHECK_SECONDS` instead.
4. **Timer fires.** The possible outcomes:
   - Still moving: re-arm. It's an inactivity timeout, not a travel cap.
   - Satisfied: complete. This covers controllers that report only the final state.
   - Within `DEVIATION_TOLERANCE_PCT`: accept with a warning, but only for a height move with its
     target clear of the band. Slat steps and band targets must land exactly.
   - Otherwise: stall and notify, with no stop.
   - Position unreadable: stall and send a stop.
5. **Completion.** Commit the plan's latch belief and cancel the timer.

A new command replaces a plan in flight, planned from the belief as it would be after the
interruption. An intent that plans nothing leaves the running plan alone.

### Sequences

- **Enter:** `open_full`, `MoveTo(lower - epsilon)`, `MoveTo(upper)`, then `MoveTo(landing)` if the
  landing isn't `upper`.
- **Leave:** `RiseToAtLeast(release)` via `open_full`. Only from `LATCHED`.
- **Guarded descent** (close, long down, a descending height move): prefixed with `open_full` while
  `may_be_latched`.
- **Height move** (`set_position` or a step): snapped out of the band, then one `MoveTo`, guarded if
  it descends.
- **Slat move:** one `MoveTo` inside `[lower, upper]`.

```text
        enter: open fully, down to (lower - epsilon), up to upper, then to the landing
   NORMAL  ───────────────────────────────────────────────────────────►  TILT
      ▲            leave: open fully (satisfied once past release)            │
      └───────────────────────────────────────────────────────────────────────┘
```

### Invariants

`planner.check_plan` runs on every plan before it executes. A violation means a planner bug, so it
disables the blind and notifies.

- **N1:** in normal mode, nothing targets or commands strictly inside the band.
- **T1:** slat moves stay in `[lower, upper]` and are planned only from `LATCHED`.
- **L1:** a descent below `lower` is preceded by a full open unless known `UNLATCHED`.
- **E1:** only the enter sequence establishes `LATCHED`: full open, dip below `lower`, rise to
  `upper`, optional in-zone landing.
- **X1:** leaving is a single `open_full` rise satisfied no lower than `release`, only from
  `LATCHED`.
- **R1:** a normal plan commits `UNLATCHED` only if no position-commanded rise ends on `release`
  having possibly crossed the lower edge. Such a rise has zero margin, so it commits `UNKNOWN`.

N1, T1, L1 and `can_change_latch` check both the target and the commanded position.

## Tilt-Zone Math

All positions are `int`. Rounding happens only on values from Home Assistant (halves up) and in the
virtual mapping. Outside tilt, virtual = real. Inside, it is inverted:

```text
real    = upper - round(virtual * (upper - lower) / 100)
virtual = round((upper - real) * 100 / (upper - lower))
```

Real positions round-trip exactly, because one real percent is wider than one virtual percent.

Example with `upper = 44`, `lower = 38`, `epsilon = 2`, `release = 46`. Band = `[36, 46]`:

- virtual 100 → real 38 (open), virtual 0 → real 44 (closed), virtual 50 → real 41.
- Enter dips to 36, rises to 44, then moves to the landing.
- Height targets in `(36, 46)` snap to the nearer edge, ties going up. Steps snap in their direction
  of travel.

`band_high` is `release` by definition, because a latched blind can rest anywhere up to its release
height. Raising `tilt_zone_release_pct` widens everything derived from the band.

## Publishing

`logic._publish_current` runs after every event and emits one `publish_state` (position, mode,
motion) when any of the three changed. They travel together so a slat angle is never read as a
height.

- **Mode** comes from `_belief_after_interrupt`: the belief if the current plan stopped now. A slat
  move keeps tilt; an entry shows it only once latched; anything that may release (an exit, a long
  press up) drops it at once. `_displayed_in_tilt` uses the same rule for the toggle and
  `set_tilt_mode` decisions.
- **Motion** is on the virtual scale: a real descent while latched is `opening`. It comes from the
  controller, or from the current step while a command is outstanding. A settled report means
  `idle`, even while a recheck is pending.
- An unknown position is published as `unavailable`.

The adapter writes the position sensor every time. It writes the tilt-mode sensor only when it
changes, since it is mirrored to KNX. A fresh instance always writes it once, which re-creates the
entity after an HA restart. The position is sent as a string because AppDaemon drops a falsy `0`.

## Safety

- **Stops** go to the real cover only if the blind may be moving: the controller says so, or a
  command is unanswered by a settled report. On a KNX actuator without a stop object, HA sends
  `stop_cover` on the step object, which nudges an idle blind. A controller that has never
  reported motion gets a stop whenever a plan is pending, since its settled-looking reports prove
  nothing.
- **Rate limit:** more than `COMMAND_RATE_LIMIT` commands in `COMMAND_RATE_WINDOW_SECONDS` disables
  the blind until AppDaemon restarts, and notifies.
- **Error boundary:** an unhandled callback exception disables the blind and notifies. Wrapping
  callbacks in `try`/`except` is the one sanctioned exception to the project's "let errors
  propagate" rule. Malformed external input (bad `set_position`, `set_tilt_mode` without `enabled`,
  unknown command, bad KNX value, non-numeric position) is logged and ignored instead.
- **Startup:** `STARTUP_DELAY_SECONDS` after start, the adapter seeds the belief from the real
  position. Commands are ignored until then. Startup never moves the blind; an `UNKNOWN` latch is
  resolved by the full open that guards the next descent or entry.

## Home Assistant Wiring

No MQTT broker. A template cover forwards commands as `gradhermetic_command` events. The app
publishes the two entities itself, and `input_button` helpers carry step and tilt presses. All live
in the private HA config repo.

### `input_button` helpers

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

### Template cover

Goes under the `template:` key; add it to an existing `template:` include if there is one.

```yaml
template:
  - cover:
      - name: "Living Room Blind"
        unique_id: gradhermetic_living_room
        default_entity_id: cover.gradhermetic_living_room
        position: "{{ states('sensor.gradhermetic_living_room_position') | int(0) }}"
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

`default_entity_id` pins the id the dashboards expect. The app matches events by `virtual_id`, not
by entity id.

### KNX events (`knx/knx_events.yaml`)

List each blind's three wall-button addresses. In ETS these must not be linked to the actuator. No
`type:` is needed, because the adapter decodes the raw bit itself.

```yaml
- address:
    - "2/6/7"   # Playroom Shutter - long press
    - "2/7/7"   # Playroom Shutter - short press
    - "2/8/7"   # Playroom Shutter - toggle slat mode
```

### KNX expose (`knx/knx_expose.yaml`)

Mirrors both entities onto the bus. Expose them as a pair, because the position is a slat angle
while the tilt bit is on. Nothing is sent while the position is `unavailable`.

```yaml
- entity_id: "binary_sensor.gradhermetic_playroom_shutter_tilt_mode"
  address: "9/3/0"
  type: "binary"
- entity_id: "sensor.gradhermetic_playroom_shutter_position"
  address: "9/3/1"
  type: "percent"
```

## Installation

This repo is a git submodule at `apps/public_apps` in the private HA config repo. Register one
instance per blind in `apps/apps.yaml`, as shown in the README's [Configuration](README.md#configuration).

## Tests

From `apps/public_apps`:

```bash
python3 -m unittest discover -s gradhermetic_cover_control/tests -t . -v
```

| File | Covers |
|---|---|
| `test_geometry.py` | Mapping round-trips, predicates at the edges, snapping, validation |
| `test_planner.py` | Golden sequences per intent; a full state-space sweep asserting every plan passes `check_plan`; each invariant rejecting what it forbids |
| `test_executor.py` | Skip-if-satisfied, stale-feedback immunity, every settle-timer path, the recheck |
| `test_logic.py` | Event surface, belief transitions, replacement, step rule, publishing, tilt toggle, named regressions |
| `test_config.py`, `test_runtime.py` | `apps.yaml` parsing, rate limiter |
| `test_adapter.py` | The adapter against a fake `hass.Hass`: wiring, filtering, startup gate, malformed input, button edges, action translation, rate limit, error boundary |
| `simulator.py`, `test_model.py` | Whole-app model tests, below |

`simulator.py` is an independent model of the mechanism, as pessimistic as the hardware allows:

- any upward crossing of the lower edge latches;
- only reaching `release` releases;
- descending changes nothing.

It records a **violation** whenever it is driven below the lower edge while latched. It can simulate
controller quirks: duplicate reports, no motion state, final report only, a stale echo of the start
position, and calibration drift.

`test_model.py` sweeps every intent from every position, every slat position, every interrupted
sequence and every quirk, on the default geometry and a custom release/landing geometry. Every run
must end with:

- zero violations;
- a belief never more confident than the truth;
- tilt mode never shown while unlatched;
- nominal flows completing without the settle timer firing;
- a bounded number of commands.

On drift: calibration error clears only at a limit, and entry issues up to three position commands
before any slat move. So the tolerable per-move error is about a third of `tilt_zone_epsilon_pct`.
Beyond that, re-measure the zone.
