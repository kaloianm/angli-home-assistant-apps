# Extractor Fan Control Implementation

This app is split into a pure state machine and a thin AppDaemon adapter.

- `logic.py` contains `ExtractorFanPairLogic`, which receives light events, schedule events, and
  timer ticks. It returns declarative actions.
- `extractor_fan_control.py` adapts those actions to AppDaemon listeners, timers, Home Assistant
  services, and persistent notifications.
- `config.py` parses and validates `apps.yaml` configuration.
- `runtime.py` stores AppDaemon callback handles and safety counters.

## State Machine

Each configured light/fan pair has independent runtime state.

```text
IDLE --(light on)--> WAITING_FOR_ACTIVATION --(threshold reached)--> RUNNING_LIGHT
  ^                         |                                      |
  |                         +--(light off before threshold)--------+
  |                                                                |
  +--(short visit light off)---------------------------------------+
                                                                   |
                                                                   +--(long visit light off)--> POST_RUN --(deadline)--> IDLE
                                                                   |
daily schedule --> SCHEDULED_RUN --(deadline)--> IDLE              |
        |                                                          |
        +---------- overlap with light/post-run ----------> COMBINED_RUN
```

Fan output is a pure merge of active demand: the fan runs whenever occupancy or schedule demand is
active, and is off otherwise. The named states above are derived from that demand for logging and
readability. Because a post-run window only starts while occupancy was already active (fan already
running), a light-off event can never turn the fan on — it can only keep it running or turn it off.

Internal states:

- `IDLE`: no light, occupancy, or schedule demand is active.
- `WAITING_FOR_ACTIVATION`: light is on, but has not stayed on long enough to start the fan.
- `RUNNING_LIGHT`: light-based occupancy demand is active while the light remains on.
- `POST_RUN`: light has turned off after a long visit; fan remains on until the computed post-run
  deadline.
- `SCHEDULED_RUN`: daily freshness run is active without light-based demand.
- `COMBINED_RUN`: schedule and occupancy/post-run demand overlap.
- `DISABLED`: the pair has been disabled until restart after an error or safety limit.

## KNX Keepalive

The fan switch is expected to be backed by a KNX staircase function. While automation requires the
fan to run, the app sends periodic ON pulses at:

```text
staircase_interval_seconds - pulse_guard_seconds
```

For a 30 second staircase timer and 5 second guard, the app sends an ON pulse every 25 seconds.

The app fully owns the fan switch and does not listen to it, so it never has to disambiguate its own
KNX feedback from a manual user toggle.

## Safety Behavior

Each pair has a fan command rate limit. If the app sends more than five fan switch commands for one
pair in 30 seconds, that pair is disabled until AppDaemon restarts and a Home Assistant persistent
notification is created.

Unhandled callback exceptions also disable the affected pair until restart and create a persistent
notification.

## KNX Actuator Settings

The relay controlling the fan should be configured roughly as:

- Feedback ON
- Time delays OFF
- Staircase function ON
- Staircase time matching `staircase_interval_seconds`
- Staircase time retriggerable ON
- Switch-on delay OFF
- Reaction to OFF telegram: switch off
- End of staircase time: switch off

## Installation

This repository is used as a git submodule under `apps/public_apps` in the private Home Assistant
config repo. AppDaemon resolves the module path as
`extractor_fan_control.extractor_fan_control` from that location.

## Running Tests

From the repository root:

```bash
python3-venv/bin/python -m pytest extractor_fan_control/tests/ -v
```

Tests cover light-based state transitions, post-run deadlines, daily schedule overlap, the
invariant that a light-off never starts the fan, config validation, and the runtime fan command
rate limit. No AppDaemon installation is required to run them.
