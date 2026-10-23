# Extractor Fan Control Implementation

User-facing behavior and configuration are in [README.md](README.md).

## Architecture

| Module | Responsibility |
|---|---|
| `logic.py` | `ExtractorFanPairLogic`: pure state machine for one pair, returning declarative actions |
| `extractor_fan_control.py` | AppDaemon adapter: listeners, timers, service calls, notifications |
| `config.py` | Parses `apps.yaml` |
| `runtime.py` | Per-pair callback handles and the fan command rate limiter |

## State Machine

The logic tracks two demand sources per pair:

- **Occupancy:** active while the light is on past activation, then until the post-run deadline.
- **Schedule:** active until the daily run's deadline.

The fan runs whenever either is active. A post-run only starts from active occupancy, so a light-off
can never turn the fan on. The named states are derived from the demands, and are used only for
logging:

| State | Meaning |
|---|---|
| `IDLE` | No demand |
| `WAITING_FOR_ACTIVATION` | Light on, activation time not yet reached |
| `RUNNING_LIGHT` | Occupancy demand, light on |
| `POST_RUN` | Occupancy demand after a long visit, light off |
| `SCHEDULED_RUN` | Schedule demand only |
| `COMBINED_RUN` | Both |
| `DISABLED` | Disabled until restart after an error or the rate limit |

Every event and timer tick recomputes all outputs, and an action is emitted only for outputs that
changed:

| Output | Actions |
|---|---|
| fan | `fan_on` / `fan_off` |
| keepalive | `start_keepalive` / `stop_keepalive` |
| activation timer | `set_timer` / `cancel_timer` |
| deadline timer | `set_timer` / `cancel_timer` |
| off deadline | `set_off_deadline` |

The deadline timer is set to the *earliest* active deadline, so the adapter can wake up there and
re-evaluate.

## Off-Deadline Sensor

`set_off_deadline` carries the *latest* active deadline, because that's when the fan actually stops.
It is `at=None` while the light is on with occupancy, since the post-run isn't computed until
light-off. The adapter writes it to `off_at_sensor_entity` with `set_state`, which isn't
rate-limited. The logic works in naive local time; the adapter attaches the timezone at publish
time because `device_class: timestamp` needs an aware ISO 8601 string.

`initialize()` publishes `unknown` for every pair, which creates the sensor and clears a pre-restart
countdown. Disabling a pair also publishes `unknown` directly, because once the pair is disabled the
adapter ignores the logic's actions.

## KNX Keepalive

While the fan should run, the adapter sends ON every `staircase_interval_seconds -
pulse_guard_seconds` (25 s for 30 s / 5 s). The app never listens to the fan switch, so it never has
to tell its own feedback apart from a manual toggle.

Suggested actuator relay settings:

- Feedback ON, time delays OFF, switch-on delay OFF
- Staircase function ON, retriggerable, time = `staircase_interval_seconds`
- OFF telegram and end of staircase time: switch off

## Safety

- **Rate limit:** more than `FAN_CMD_RATE_LIMIT` (5) fan commands in `FAN_CMD_RATE_WINDOW_SECONDS`
  (30), keepalives included, disables the pair until restart and creates a persistent notification.
- **Error boundary:** an unhandled callback exception disables the affected pair and notifies.

## Installation

This repo is a git submodule at `apps/public_apps` in the private HA config repo. See the README's
[Configuration](README.md#configuration) section for the `apps.yaml` entry.

## Tests

From `apps/public_apps`:

```bash
python3 -m unittest discover -s extractor_fan_control/tests -t . -v
```

The tests cover:

- light-driven transitions and post-run deadlines
- schedule overlap
- the off-deadline output
- the rule that a light-off never starts the fan
- config validation
- the rate limiter

No AppDaemon installation is needed.
