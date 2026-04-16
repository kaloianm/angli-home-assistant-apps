# gradhermetic-appdaemon

An [AppDaemon](https://appdaemon.readthedocs.io/) application that wraps a regular (time-based relay) Home Assistant cover entity and exposes a new **MQTT cover with full tilt support**, modelling the master-slat pin engagement sequence of Gradhermetic Supergradhermetic roller blinds.

## How it works

Your existing HA cover entity (the relay controller) becomes the motor backend.
This app creates a virtual cover entity via MQTT auto-discovery that adds:

- **Tilt slider** — drag to orient slats; the app automatically runs the mechanical engagement sequence on the real cover before adjusting slat angle.
- **Enter Slat Mode button** — explicitly triggers the engagement sequence.
- **Slat Step Up / Slat Step Down buttons** — incremental slat adjustment.

### Engagement sequence (automated)

When a tilt command arrives and tilt is not yet engaged, the app:

1. Moves the real cover down to `tilt_lower − ε` (reaches the pin).
2. Moves the real cover up to `tilt_upper + ε` (locks the pin).
3. Tilt is now active — subsequent position commands on the real cover within `[tilt_lower, tilt_upper]` control slat orientation.

Sending a normal Open / Close / Set Position command on the virtual cover automatically exits tilt mode.

## Requirements

- Home Assistant with the **MQTT** integration
- An MQTT broker (e.g. Mosquitto / HA Mosquitto add-on)
- AppDaemon 4.x with the **MQTT plugin** enabled

## Installation

### 1. Copy the app

Copy `apps/gradhermetic_cover.py` into your AppDaemon `apps/` directory.

### 2. Configure the MQTT plugin

In your `appdaemon.yaml`, make sure the MQTT plugin is present:

```yaml
plugins:
  HASS:
    type: hass
  MQTT:
    type: mqtt
    namespace: mqtt
    client_host: 192.168.1.X      # your MQTT broker
    client_port: 1883
    # client_user: "user"
    # client_password: "pass"
```

### 3. Add app configuration

Copy `apps.yaml.example` to `apps.yaml` (or merge into your existing one) and adjust the values:

```yaml
gradhermetic_living_room:
  module: gradhermetic_cover
  class: GradhhermeticCover
  real_cover: cover.living_room_blind
  virtual_id: living_room
  virtual_name: "Living Room Blind"
  tilt_lower_pct: 3.0
  tilt_upper_pct: 10.0
  epsilon_pct: 2.0
  full_travel_time_secs: 60.0
  step_pct: 5.0
  tilt_step_pct: 10.0
```

### 4. Restart AppDaemon

The virtual cover and button entities will appear automatically in HA via MQTT discovery.

## Exposed entities

For a blind with `virtual_id: living_room`:

| Entity                                        | Type    | Description                        |
|-----------------------------------------------|---------|------------------------------------|
| `cover.gradhermetic_living_room`              | Cover   | Virtual cover with tilt support    |
| `button.gradhermetic_living_room_enter_slat`  | Button  | Trigger tilt engagement            |
| `button.gradhermetic_living_room_slat_step_up`| Button  | Step slats toward closed           |
| `button.gradhermetic_living_room_slat_step_down`| Button | Step slats toward open            |

## Configuration reference

| Key                    | Default | Description                                              |
|------------------------|---------|----------------------------------------------------------|
| `real_cover`           | —       | Entity ID of the real relay-based cover (required)       |
| `virtual_id`           | —       | Short ID for MQTT topics and entity naming               |
| `virtual_name`         | —       | Friendly name shown in HA                                |
| `tilt_lower_pct`       | 3.0     | Bottom of the tilt zone (%)                              |
| `tilt_upper_pct`       | 10.0    | Top of the tilt zone (%)                                 |
| `epsilon_pct`          | 2.0     | Overshoot for pin engagement (%)                         |
| `full_travel_time_secs`| 60.0    | Total motor travel time 0→100% (seconds)                 |
| `step_pct`             | 5.0     | Position step size (%)                                   |
| `tilt_step_pct`        | 10.0    | Tilt step size (%)                                       |

## Architecture

The project is split into two layers:

- **`apps/gradhermetic_logic.py`** — Pure business logic with zero framework dependencies. The `BlindController` class accepts commands and returns `Action` dataclasses describing what side effects to perform (move cover, schedule timer, publish state, etc.). This is fully unit-testable without AppDaemon or Home Assistant.

- **`apps/gradhermetic_cover.py`** — Thin AppDaemon adapter that wires the controller to MQTT and HA services. Its `_execute()` method maps each `Action` to the appropriate framework call.

## Testing

### Unit tests (no infrastructure needed)

Unit tests exercise the `BlindController` logic directly — no MQTT broker, no Home Assistant, no AppDaemon runtime.

```bash
pip install -r requirements-test.txt
pytest tests/test_logic.py -v
```

### Integration tests (against the Rust emulator)

Integration tests drive the `BlindController` against the real Rust emulator over MQTT. This validates the full engagement sequence end-to-end.

Requirements:
- **MQTT broker** — provided automatically via one of:
  - **Docker** (preferred): `pytest-mqtt` auto-starts an `eclipse-mosquitto` container if Docker is running.
  - **Manual Docker**: start one yourself if you prefer:
    ```bash
    docker run -d --name mosquitto -p 1883:1883 eclipse-mosquitto:2 mosquitto -c /mosquitto-no-auth.conf
    ```
  - **System install**: any broker already listening on `localhost:1883` is auto-detected and reused.
- The emulator binary built from the sibling `emulator` project

```bash
# Build the emulator first
cd ../emulator
cargo build --release

# Run integration tests
cd ../appdaemon
pip install -r requirements-test.txt
pytest tests/test_integration.py -v --timeout=30

# Or point to a custom emulator path / MQTT host:
EMULATOR_BIN=/path/to/emulator \
pytest tests/test_integration.py --mqtt-host 192.168.1.10 -v
```

Integration tests are automatically skipped when the emulator binary is not found.

## License

MIT
