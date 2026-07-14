# Gradhermetic Cover Control Implementation

This app is split into a pure state machine and a thin AppDaemon adapter.

- `logic.py` contains `GradhermeticCoverLogic`, which receives user, command, KNX, and
  position-feedback events and returns declarative actions. It owns the tilt-zone math and the latch
  sequencing.
- `gradhermetic_cover_control.py` adapts those actions to AppDaemon: listening for command and KNX
  events, the custom service, real-cover service calls, position display, and persistent
  notifications.
- `config.py` parses and validates one blind's `apps.yaml` configuration.
- `runtime.py` stores AppDaemon callback handles and a command rate limiter.

Each app instance manages exactly one blind.

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

## State Machine

The blind is either under whole-height control (`NORMAL`) or latched for slat control (`TILT`).
Latching requires a full down-then-up motion across the lower edge, so every command is compiled to
an ordered list of waypoints — a **movement plan** — that the adapter drives one at a time.

```text
                 enter: down to (lower - epsilon), then up to upper
   NORMAL  ───────────────────────────────────────────────────────►  TILT
 (height control)                                              (slat control, latched)
      ▲                    leave: up to (upper + epsilon)                │
      └────────────────────────────────────────────────────────────────┘
                       (disengage is always upward)
```

The logic tracks:

- `last_position` — the blind's real travel position (0-100), from controller feedback.
- `in_tilt` — whether the mechanism is believed latched.
- `is_moving` — whether the blind is currently travelling, from controller feedback.
- the pending movement plan, if any.

The enter sequence assumes it starts above the lower edge, but the caller may start anywhere. If the
start position lies inside the ambiguity band (`[lower - epsilon, upper + epsilon]`) the latch might
already be engaged — an entry interrupted mid-rise physically latches the moment the rise crosses the
lower edge — so `_enter_tilt` first rises above the zone to release the latch before dipping, since
the latch only releases upward. From clearly below the band it hops just above the lower edge; from an
unknown position it recovers fully open. Only then does it dip below the lower edge and rise to latch.

A plan advances only when `on_real_position` reports the current waypoint reached (within
`POSITION_TOLERANCE_PCT`) **and** the blind has settled (state no longer `opening`/`closing`). When
the last waypoint completes, the terminal `in_tilt` is committed and the virtual position is
published to `sensor.gradhermetic_<virtual_id>_position`. The adapter arms a
`SETTLE_TIMEOUT_SECONDS` fallback timer per move in case a final position update is never observed.
The timer is an **inactivity** timeout, not a hard travel-time cap: if it fires while the blind is
still reporting motion the move is simply long, so it re-arms and keeps waiting rather than declaring
a stall.

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
- entering dips to `lower - epsilon = 36`, then rises to `44` to latch.
- leaving rises to `upper + epsilon = 46`.

Because the zone is narrow (6% here) and KNX actuators report integer positions, the zone holds only
about `span + 1` distinct slat positions (~7 for a 6% zone). A slat step must therefore map to at
least one whole reported percent of real travel, otherwise the rounded position command repeats the
current position and the blind never moves. Config validation enforces
`tilt_step_pct >= 100 / (upper - lower)` (≈16.7 here) so every step advances the actuator.

## KNX Wall-Button Handling

Two dedicated group addresses drive the app as `knx_event`s (telegram value `0 = up / more light`,
`1 = down / less light`, matching the repo convention):

- **Move address** — long presses. Long up drives fully open (leaving tilt naturally); long down
  drives fully closed (rising out of the zone first if latched, since the latch releases only
  upward).
- **Step address** — short presses, evaluated in priority order:
  1. If the blind is moving, stop it.
  2. Otherwise, if latched, step the slats by `tilt_step_pct` (up toward open, down toward closed).
     An up step at the open edge leaves tilt upward and resumes whole-height control.
  3. Otherwise (idle, outside the zone), enter tilt when the press points toward the zone: a down
     press from above enters at the most-closed edge; an up press from below enters at the most-open
     edge. A press pointing away does nothing (long press covers the extremes).

## Virtual Cover Wiring

Commands reach the app as a `gradhermetic_command` event carrying `virtual_id` and `command`
(`open` / `close` / `stop` / `set_position` with `position`, or `set_tilt_mode` with `enabled`). The
app filters by `virtual_id` and routes each to the logic engine. Position is reflected back with
`set_state` on `sensor.gradhermetic_<virtual_id>_position`, which the template cover displays.

Step and tilt reach the app as `input_button` presses. The app watches
`input_button.gradhermetic_<virtual_id>_step_up` / `_step_down` and routes each to `on_slat_step`
(up/down), and `..._tilt`, which toggles tilt mode (`on_set_tilt_mode` with the negation of the
current `in_tilt`). `on_slat_step` is slat-only: it steps the angle by `tilt_step_pct` and clamps at
both zone edges when latched, and is a no-op when not latched or while a plan is in flight. It
deliberately differs from a KNX wall-button short press (`on_knx_short`), which additionally crosses
the zone boundaries (entering from outside, leaving upward at the open edge) because a two-button
wall switch has no separate tilt control. The UI does — `..._tilt` — so its step helpers stay pure.

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

State is **not** persisted across restarts. `RECOVERY_DELAY_SECONDS` after startup the app reads the
real cover's position and establishes a safe state:

- position clearly **outside** the tilt zone (beyond `lower - epsilon` … `upper + epsilon`): the
  blind cannot be latched, so whole-height control resumes from that position.
- position **inside or near** the zone, or unknown: the latch state is ambiguous, so the app drives a
  single `cover.open_cover` and resets to fully open / normal — an **upward-only** recovery that
  protects the mechanism from accidental downward movement near the tilt zone.

## Safety Behavior

Each blind has a real-cover command rate limit. If the app sends more than `COMMAND_RATE_LIMIT`
commands within `COMMAND_RATE_WINDOW_SECONDS` (guarding against a plan whose waypoint is never
reached), the blind is disabled until AppDaemon restarts and a Home Assistant persistent notification
is created.

The `SETTLE_TIMEOUT_SECONDS` fallback timer only declares a stall — stopping the blind and raising an
obstruction notification — when the blind has **settled** short of its target. A move still reporting
motion when the timer fires is treated as merely long: the timer re-arms and waits, so a slow travel
never triggers a false stall. A pending plan whose position has become unreadable (the cover went
unavailable) is treated as a genuine stall rather than being left to hang silently.

Unhandled callback exceptions likewise disable the blind until restart and create a persistent
notification. Wrapping callbacks in `try/except` is the one sanctioned exception to the project's
"let errors propagate" rule — it applies only at the AppDaemon callback boundary.

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

  knx_move_address: "2/6/0"
  knx_step_address: "2/6/1"
```

## Running Tests

From the `apps/public_apps` directory:

```bash
python3 -m unittest discover -s gradhermetic_cover_control/tests -t . -v
```

Tests cover the tilt-zone mapping, the enter/leave latch sequences, whole-height and slat commands,
the short-press priority order and boundary crossings, long-press extremes, restart-recovery
decisions, config validation, and the runtime command rate limit. No AppDaemon installation is
required to run them.
