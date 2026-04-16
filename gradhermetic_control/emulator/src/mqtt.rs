// Runtime layer: MQTT connectivity, Home Assistant auto-discovery, and the
// two concurrent loops that drive the emulator.
//
// Architecture:
//   1. Simulation tick loop (spawned task) — advances blind physics every 50ms
//      and publishes state changes to MQTT.
//   2. MQTT event loop (main task) — receives incoming commands from HA and
//      dispatches them to the blind state machines.
//
// The two loops share the blind state via Arc<Mutex<HashMap>>. Commands flow
// in through the event loop, state flows out through the tick loop.

use crate::blind::{Blind, BlindSnapshot, MotorDirection};
use crate::blind_params::BlindParams;
use crate::config::Config;
use rumqttc::{AsyncClient, Event, LastWill, MqttOptions, Packet, QoS};
use serde_json::json;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Mutex;
use tokio::time;
use tracing::{error, info, warn};

/// Shared mutable blind state. Arc allows both loops to hold a reference;
/// Mutex ensures only one loop touches the blinds at a time.
type Blinds = Arc<Mutex<HashMap<String, Blind>>>;

const TICK_MS: u64 = 50;
/// Force a state publish every N ticks even if nothing changed, so HA stays
/// in sync after reconnections or missed messages.
const FORCE_PUBLISH_EVERY: u64 = 5; // ~250 ms

pub async fn run(config: Config) -> anyhow::Result<()> {
    let mut mqttopts = MqttOptions::new(
        &config.mqtt.client_id,
        &config.mqtt.host,
        config.mqtt.port,
    );
    mqttopts.set_keep_alive(Duration::from_secs(30));

    if let (Some(user), Some(pass)) = (&config.mqtt.username, &config.mqtt.password) {
        mqttopts.set_credentials(user, pass);
    }

    // Last Will: the broker auto-publishes "offline" if this client disconnects
    // unexpectedly, so HA immediately marks the entities as unavailable.
    mqttopts.set_last_will(LastWill::new(
        "gradhermetic/status",
        Vec::from("offline"),
        QoS::AtLeastOnce,
        true,
    ));

    // rumqttc splits the MQTT client into two halves:
    //   client    — used to publish messages and subscribe to topics
    //   eventloop — must be polled continuously to drive the connection
    // The 256 is the channel capacity for outgoing messages.
    let (client, mut eventloop) = AsyncClient::new(mqttopts, 256);

    let blinds: Blinds = Arc::new(Mutex::new(HashMap::new()));
    for bcfg in &config.blinds {
        let params = BlindParams::new(
            bcfg.full_travel_time_secs,
            bcfg.tilt_lower_pct,
            bcfg.tilt_upper_pct,
            bcfg.epsilon_pct,
            bcfg.step_pct,
            bcfg.tilt_step_pct,
        )
        .map_err(|e| anyhow::anyhow!("Blind '{}': {}", bcfg.id, e))?;
        let blind = Blind::new(params);
        blinds.lock().await.insert(bcfg.id.clone(), blind);
    }

    // HA MQTT Discovery: publish a retained JSON config for each blind to
    // `homeassistant/cover/gradhermetic/{id}/config`. HA picks these up
    // automatically — no manual YAML entity configuration needed.
    for bcfg in &config.blinds {
        publish_discovery(&client, bcfg).await?;
    }

    // Subscribe to the three command topics per blind that HA will publish to.
    for bcfg in &config.blinds {
        let pfx = format!("gradhermetic/{}", bcfg.id);
        for suffix in ["set", "position/set", "tilt/set"] {
            client
                .subscribe(format!("{pfx}/{suffix}"), QoS::AtLeastOnce)
                .await?;
        }
    }

    // Mark ourselves as online (retained). Combined with the Last Will above,
    // this implements HA's availability protocol.
    client
        .publish("gradhermetic/status", QoS::AtLeastOnce, true, "online")
        .await?;

    info!(
        "Gradhermetic emulator started — {} blind(s)",
        config.blinds.len()
    );

    // ── Simulation tick loop (spawned as a concurrent task) ──────────
    //
    // Clones of Arc/client are moved into the spawned task so both loops
    // can independently access the shared blind state and MQTT client.
    let blinds_sim = blinds.clone();
    let client_sim = client.clone();
    let ids: Vec<String> = config.blinds.iter().map(|b| b.id.clone()).collect();

    tokio::spawn(async move {
        let mut interval = time::interval(Duration::from_millis(TICK_MS));
        let mut counter: u64 = 0;
        // Tracks the last published snapshot per blind to avoid redundant
        // MQTT publishes when state hasn't meaningfully changed.
        let mut prev: HashMap<String, BlindSnapshot> = HashMap::new();

        loop {
            interval.tick().await;
            counter += 1;
            let force = counter >= FORCE_PUBLISH_EVERY;
            if force {
                counter = 0;
            }

            let mut blinds = blinds_sim.lock().await;
            for id in &ids {
                if let Some(blind) = blinds.get_mut(id) {
                    let changed = blind.tick();
                    // Publish on actual state change OR on forced heartbeat,
                    // but still deduplicate against the last published snapshot.
                    if changed || force {
                        let snap = blind.snapshot();
                        if prev.get(id) != Some(&snap) {
                            if let Err(e) = publish_state(&client_sim, id, &snap).await {
                                error!("publish {id}: {e}");
                            }
                            prev.insert(id.clone(), snap);
                        }
                    }
                }
            }
        }
    });

    // ── MQTT event loop (runs on the main task) ────────────────────
    //
    // eventloop.poll() must be called continuously — it drives the
    // underlying TCP connection, handles MQTT keepalives, and delivers
    // incoming messages. We only act on Publish packets (commands from HA);
    // all other events (ConnAck, PingResp, etc.) are ignored.
    loop {
        match eventloop.poll().await {
            Ok(Event::Incoming(Packet::Publish(p))) => {
                let topic = p.topic.clone();
                let payload = String::from_utf8_lossy(&p.payload).to_string();
                dispatch_command(&blinds, &topic, &payload).await;
            }
            Ok(_) => {}
            Err(e) => {
                error!("MQTT connection error: {e}");
                time::sleep(Duration::from_secs(5)).await;
            }
        }
    }
}

// ── HA MQTT Discovery ──────────────────────────────────────────────────
//
// Home Assistant's MQTT integration watches for retained messages on
// `homeassistant/<component>/<node_id>/config`. Publishing a JSON payload
// there makes HA auto-create an entity with the specified topics and
// capabilities. The "~" key is a base-topic shorthand — HA expands "~/foo"
// to "<prefix>/foo" in all topic fields.

async fn publish_discovery(
    client: &AsyncClient,
    bcfg: &crate::config::BlindInstanceConfig,
) -> anyhow::Result<()> {
    let prefix = format!("gradhermetic/{}", bcfg.id);
    let discovery = json!({
        "~": prefix,
        "name": bcfg.name,
        "unique_id": format!("gradhermetic_{}", bcfg.id),
        "object_id": format!("gradhermetic_{}", bcfg.id),
        "command_topic": "~/set",
        "state_topic": "~/state",
        "position_topic": "~/position",
        "set_position_topic": "~/position/set",
        "tilt_status_topic": "~/tilt",
        "tilt_command_topic": "~/tilt/set",
        "availability_topic": "gradhermetic/status",
        "payload_available": "online",
        "payload_not_available": "offline",
        "payload_open": "OPEN",
        "payload_close": "CLOSE",
        "payload_stop": "STOP",
        "position_open": 100,
        "position_closed": 0,
        "tilt_opened_value": 100,
        "tilt_closed_value": 0,
        "tilt_min": 0,
        "tilt_max": 100,
        "device_class": "blind",
        "device": {
            "identifiers": [format!("gradhermetic_{}", bcfg.id)],
            "name": &bcfg.name,
            "manufacturer": "Gradhermetic",
            "model": "Supergradhermetic (Emulated)",
        },
    });

    let topic = format!("homeassistant/cover/gradhermetic/{}/config", bcfg.id);
    client
        .publish(topic, QoS::AtLeastOnce, true, discovery.to_string())
        .await?;

    info!("Published MQTT discovery for '{}'", bcfg.id);
    Ok(())
}

// ── State publishing ───────────────────────────────────────────────────
//
// Publishes three retained topics per blind that HA reads:
//   .../state    — "open" | "closed" | "opening" | "closing" (for the icon/state badge)
//   .../position — integer 0–100 (drives the position slider)
//   .../tilt     — integer 0–100 (drives the tilt slider; 100 = open when not in tilt mode)

async fn publish_state(
    client: &AsyncClient,
    id: &str,
    snap: &BlindSnapshot,
) -> anyhow::Result<()> {
    let pfx = format!("gradhermetic/{id}");

    // HA state values: "opening"/"closing" while moving, "open"/"closed" when
    // stopped. A partially-open stopped blind reports as "open" since HA has
    // no "partially open" state — the position slider conveys the exact value.
    let state_str = match snap.motor {
        MotorDirection::Up => "opening",
        MotorDirection::Down => "closing",
        MotorDirection::Stopped if snap.position >= 99.5 => "open",
        MotorDirection::Stopped if snap.position <= 0.5 => "closed",
        MotorDirection::Stopped => "open",
    };

    client
        .publish(
            format!("{pfx}/state"),
            QoS::AtLeastOnce,
            true,
            state_str,
        )
        .await?;

    client
        .publish(
            format!("{pfx}/position"),
            QoS::AtLeastOnce,
            true,
            format!("{:.0}", snap.position),
        )
        .await?;

    // Publish tilt: use 100 (open) as default when not in tilt mode.
    let tilt_val = snap.tilt.unwrap_or(100.0);
    client
        .publish(
            format!("{pfx}/tilt"),
            QoS::AtLeastOnce,
            true,
            format!("{:.0}", tilt_val),
        )
        .await?;

    Ok(())
}

// ── Command dispatch ───────────────────────────────────────────────────
//
// Incoming MQTT messages are routed by topic structure:
//   gradhermetic/{blind_id}/set          → OPEN / CLOSE / STOP
//   gradhermetic/{blind_id}/position/set → float 0–100
//   gradhermetic/{blind_id}/tilt/set     → float 0–100
// The blind_id is extracted from the topic path, not the payload.

async fn dispatch_command(blinds: &Blinds, topic: &str, payload: &str) {
    let parts: Vec<&str> = topic.split('/').collect();
    if parts.len() < 3 || parts[0] != "gradhermetic" {
        return;
    }

    let blind_id = parts[1];
    let mut guard = blinds.lock().await;
    let blind = match guard.get_mut(blind_id) {
        Some(b) => b,
        None => {
            warn!("Command for unknown blind '{blind_id}'");
            return;
        }
    };

    let payload = payload.trim();
    match &parts[2..] {
        ["set"] => {
            info!("{blind_id}: command {payload}");
            match payload {
                "OPEN" => blind.cmd_open(),
                "CLOSE" => blind.cmd_close(),
                "STOP" => blind.cmd_stop(),
                other => warn!("{blind_id}: unknown command '{other}'"),
            }
        }
        ["position", "set"] => {
            if let Ok(pos) = payload.parse::<f64>() {
                info!("{blind_id}: set_position {pos}");
                blind.cmd_set_position(pos);
            }
        }
        ["tilt", "set"] => {
            if let Ok(tilt) = payload.parse::<f64>() {
                info!("{blind_id}: set_tilt {tilt}");
                blind.cmd_set_tilt(tilt);
            }
        }
        _ => {}
    }
}
