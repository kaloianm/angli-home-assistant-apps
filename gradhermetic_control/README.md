# GradhermeticCover

GradhermeticCover wraps a regular relay-based Home Assistant cover and exposes
an MQTT virtual cover with Gradhermetic slat/tilt behavior.

## Overall behavior

- Open / Close / Stop / Set Position commands on the virtual cover are
  translated to commands on the real cover.
- Tilt commands trigger the Gradhermetic engagement sequence when needed:
  move down to `tilt_lower - epsilon`, then up to `tilt_upper + epsilon`.
- Once engaged, positions within the tilt zone control slat orientation.
- Sending a regular Open / Close / Set Position command exits tilt mode.
- MQTT discovery entities are published automatically for:
  - virtual cover
  - Enter Slat Mode button
  - Slat Step Up button
  - Slat Step Down button
- Runtime logic is isolated in `BlindController`; AppDaemon code is a thin
  adapter around MQTT, timers, and HA services.

## YAML configuration example

```yaml
# One block per physical blind:
gradhermetic_living_room:
  module: gradhermetic_cover
  class: GradhermeticCover

  # Existing HA cover entity controlled by your relay/motor backend
  real_cover: cover.living_room_blind

  # Identity for the virtual MQTT cover
  virtual_id: living_room
  virtual_name: "Living Room Blind"

  # Tilt mechanism parameters
  tilt_lower_pct: 3.0
  tilt_upper_pct: 10.0
  epsilon_pct: 2.0

  # Total travel time for 0 -> 100%
  full_travel_time_secs: 60.0

  # Step sizes for regular position and tilt step commands
  step_pct: 5.0
  tilt_step_pct: 10.0

# gradhermetic_bedroom:
#   module: gradhermetic_cover
#   class: GradhermeticCover
#   real_cover: cover.bedroom_blind
#   virtual_id: bedroom
#   virtual_name: "Bedroom Blind"
#   tilt_lower_pct: 3.0
#   tilt_upper_pct: 10.0
#   epsilon_pct: 2.0
#   full_travel_time_secs: 45.0
#   step_pct: 5.0
#   tilt_step_pct: 10.0
```

## Tests

From `gradhermetic_control/`:

```bash
python3 -m venv python3-venv
source python3-venv/bin/activate
pip install -r tests/requirements-test.txt
pytest tests/test_logic.py -v
```

Integration tests use MQTT + the Rust emulator:

```bash
# Build the emulator first
cd emulator
cargo build --release

# Back to gradhermetic_control/
cd ../
pytest tests/test_integration.py -v --timeout=30

# Optional: custom emulator binary path / MQTT host
EMULATOR_BIN=/path/to/emulator \
pytest tests/test_integration.py --mqtt-host 192.168.1.10 -v
```
