# Shelly Device Scripts

Scripts that run **on** Shelly devices, rather than in AppDaemon. They are deployed to device
firmware by an accompanying `deploy_*.py`, and kept in git so a device can be rebuilt or audited.

## `cover_step.js`

Gives any Shelly-controlled cover a KNX-style **step** lever.

A KNX blind actuator nudges a blind by pulsing the motor for a short, actuator-timed interval,
exposed on its Stop/Step communication object. Home Assistant's Shelly integration offers no
equivalent — its cover entity supports only absolute positioning, and since positions are integers,
the smallest possible move is 1% of travel. On a 20-second blind that is 200 ms of motion, twice a
typical KNX step. The Shelly `Cover.Open` and `Cover.Close` RPC methods do accept a `duration`,
however, with a floor of 0.1 s, so the device can time exactly the same pulse itself. This script
exposes that as two buttons.

`deploy_cover_step.py` creates two virtual buttons with `Virtual.Add`, pinning their ids so the script
can hardcode the component keys `button:200` and `button:201`. The script watches the event bus for a
`single_push` on either and pulses the cover accordingly. Home Assistant's Shelly integration
discovers the buttons and surfaces:

- `button.<device>_step_up`
- `button.<device>_step_down`

Each button is created with `meta.ui.view` set to `button`. This matters: Home Assistant maps a
virtual component to a platform only when its view is one of the modes it recognises for that
platform, so a button without the view is **hidden** — it appears neither on the device's web UI home
page nor as an entity. The deployer rewrites the config of a button that already exists, so
re-running it repairs one that was created without the view.

The deployer is specific to this script, not a general-purpose uploader: it provisions those two
buttons by id. A future device script gets its own `deploy_*.py` alongside it.

On the device the script is listed as **Cover Step** (`SCRIPT_NAME` in the deployer). That name is
also the key the deployer matches on to overwrite the script in place, so changing it strands the
previously deployed copy under its old name — still enabled, still running, still handling the same
button events. Delete the old one, or every press fires twice.

Set `STEP_SECONDS` to match the step time of the other blinds in the installation, so every cover
feels identical under the same wall button. If you change the button ids, change them in both files —
nothing asserts that they agree.

The script is deliberately dumb: it is a lever, not a controller. Deciding *when* to step — versus
stopping a moving blind, or crossing a tilt-zone boundary — belongs in the consuming app, for example
[`gradhermetic_cover_control`](../gradhermetic_cover_control/README.md).

> Shelly also offers *managed* virtual components, declared in a script's `@meta` header and reached
> through `Script.getVcHandle()`. That would remove the `Virtual.Add` step, but the function is absent
> on older firmware (it fails with `Function "getVcHandle" not found!`). `Shelly.addEventHandler` is
> available everywhere, so this script uses that instead.

### Requirements

Virtual components need Gen3 firmware 1.2.0 or later (Gen2 Pro: 1.4.0 or later), and the device must
be in cover mode with a `cover:0` component. The deployer checks for the cover.

### Deploying

Standard library only — no virtualenv needed.

```bash
./deploy_cover_step.py <host>
```

If the device has authentication enabled, export the password first — Shelly Gen2+ RPC uses HTTP
digest auth, and the username is always `admin`:

```bash
SHELLY_PASSWORD=... ./deploy_cover_step.py <host>
```
