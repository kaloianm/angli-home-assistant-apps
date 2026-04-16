# gradhermetic-emulator

A Rust-based emulator for **Gradhermetic Supergradhermetic** adjustable/orientable roller blinds, exposed to [Home Assistant](https://www.home-assistant.io/) via MQTT auto-discovery.

The emulator faithfully models the single-motor tilt-engagement mechanism: a "master slat" pin that engages when the blind is lowered past a threshold and then raised back through the tilt zone, after which motor movement controls slat orientation instead of blind height.

## How the mechanism works

```
Position (0 %  = fully closed / down,  100 % = fully open / up)
 ┌─────────────────────────────────────────────────────────────────┐
 │ 0 %                                                       100 % │
 │  ▼ engage point           tilt zone             disengage ▲     │
 │  (tilt_lower − ε) ── [tilt_lower ──── tilt_upper] ── (tilt_upper + ε)
 └─────────────────────────────────────────────────────────────────┘
```

1. **Phase 1** — Blind lowers to `tilt_lower − ε` (the engage point).
2. **Phase 2** — Blind raises to `tilt_upper + ε`; the mechanical pin locks.
3. **Tilt active** — Motor movement within `[tilt_lower, tilt_upper]` now controls slat angle:
   - Down → slats open (horizontal)
   - Up   → slats close (vertical)
4. Moving above `tilt_upper` exits tilt mode.
5. Safety: the blind cannot descend below `tilt_lower` while in tilt mode.

## HA integration

The emulator registers each configured blind as an **MQTT Cover** entity with full tilt support.  HA auto-discovers the entities — no YAML configuration on the HA side is needed beyond having the MQTT integration enabled.

### Supported HA controls

| HA action              | MQTT topic suffix  | Behaviour                                  |
|------------------------|--------------------|--------------------------------------------|
| Open / Close / Stop    | `set`              | Normal height movement                     |
| Set position           | `position/set`     | Move to exact height (exits tilt)          |
| Set tilt position      | `tilt/set`         | Engage tilt (if needed) and orient slats   |
| Open / Close tilt step | (via `tilt/set`)   | HA sends computed tilt value               |

## Quick start

```bash
# 1. Clone & build
git clone https://github.com/YOUR_USER/gradhermetic-emulator.git
cd gradhermetic-emulator
cargo build --release

# 2. Create config
cp config.example.yaml config.yaml
# Edit config.yaml — set your MQTT broker address, blind names, tilt parameters.

# 3. Run
./target/release/gradhermetic-emulator -c config.yaml
```

## Configuration

See [`config.example.yaml`](config.example.yaml) for a fully annotated reference.

| Key                    | Default | Description                                                    |
|------------------------|---------|----------------------------------------------------------------|
| `tilt_lower_pct`       | 3.0     | Bottom of the tilt zone (%)                                    |
| `tilt_upper_pct`       | 10.0    | Top of the tilt zone (%)                                       |
| `epsilon_pct`          | 2.0     | Overshoot needed to engage/disengage the pin (%)               |
| `full_travel_time_secs`| 60.0    | Total motor travel time 0 → 100 % (seconds)                   |
| `step_pct`             | 5.0     | Position step size (%)                                         |
| `tilt_step_pct`        | 10.0    | Tilt step size (%)                                             |

## Requirements

- Rust 1.70+
- An MQTT broker (e.g. Mosquitto or the Home Assistant Mosquitto add-on)
- Home Assistant with the **MQTT** integration enabled

## License

MIT
