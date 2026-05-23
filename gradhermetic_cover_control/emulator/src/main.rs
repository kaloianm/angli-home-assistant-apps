// Architecture: three modules with strict separation of concerns.
//   blind  — pure state-machine simulation (no I/O, fully unit-testable)
//   config — YAML deserialization + physical-constraint validation
//   mqtt   — MQTT connectivity, HA auto-discovery, and the runtime's two
//            concurrent loops (simulation tick + command dispatch)
mod blind;
mod blind_params;
mod config;
mod mqtt;

use clap::Parser;
use config::Config;
use std::path::PathBuf;

#[derive(Parser)]
#[command(name = "gradhermetic-emulator")]
#[command(about = "Gradhermetic roller blind emulator for Home Assistant")]
struct Cli {
    /// Path to configuration file
    #[arg(short, long, default_value = "config.yaml")]
    config: PathBuf,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();

    let cli = Cli::parse();
    let config = Config::load(&cli.config)?;

    // mqtt::run never returns — it runs the event loop forever.
    mqtt::run(config).await
}
