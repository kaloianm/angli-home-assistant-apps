// Config structs mirror the YAML file structure 1:1. serde's Deserialize derive
// maps YAML keys directly to struct fields; `#[serde(default = "...")]` provides
// sensible defaults so the YAML only needs to specify what differs from a
// typical Gradhermetic installation.

use serde::Deserialize;
use std::path::Path;

#[derive(Debug, Deserialize, Clone)]
pub struct Config {
    pub mqtt: MqttConfig,
    pub blinds: Vec<BlindInstanceConfig>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct MqttConfig {
    pub host: String,
    #[serde(default = "default_port")]
    pub port: u16,
    pub username: Option<String>,
    pub password: Option<String>,
    #[serde(default = "default_client_id")]
    pub client_id: String,
}

#[derive(Debug, Deserialize, Clone)]
pub struct BlindInstanceConfig {
    pub id: String,
    pub name: String,
    #[serde(default = "default_tilt_lower")]
    pub tilt_lower_pct: f64,
    #[serde(default = "default_tilt_upper")]
    pub tilt_upper_pct: f64,
    #[serde(default = "default_epsilon")]
    pub epsilon_pct: f64,
    #[serde(default = "default_travel_time")]
    pub full_travel_time_secs: f64,
    #[serde(default = "default_step")]
    pub step_pct: f64,
    #[serde(default = "default_tilt_step")]
    pub tilt_step_pct: f64,
}

fn default_port() -> u16 {
    1883
}
fn default_client_id() -> String {
    "gradhermetic-emulator".into()
}
fn default_tilt_lower() -> f64 {
    3.0
}
fn default_tilt_upper() -> f64 {
    10.0
}
fn default_epsilon() -> f64 {
    2.0
}
fn default_travel_time() -> f64 {
    60.0
}
fn default_step() -> f64 {
    5.0
}
fn default_tilt_step() -> f64 {
    10.0
}

impl Config {
    pub fn load(path: &Path) -> anyhow::Result<Self> {
        let content = std::fs::read_to_string(path)?;
        let config: Config = serde_yaml::from_str(&content)?;
        config.validate()?;
        Ok(config)
    }

    // Per-blind physical constraints (tilt ranges, travel time, etc.) are
    // enforced by BlindParams::new() at construction time. This method only
    // checks config-level invariants that don't belong to a single blind.
    fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(!self.blinds.is_empty(), "At least one blind must be configured");
        Ok(())
    }
}
