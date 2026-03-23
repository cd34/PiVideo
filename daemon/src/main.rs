use anyhow::{Context, Result};
use rppal::gpio::Gpio;
use serde::Deserialize;
use std::collections::HashMap;
use std::process::Command;
use std::{env, fs};

const DEFAULT_CONFIG: &str = "/opt/pivideo/config.json";
const DEFAULT_VIDEO_DIR: &str = "/opt/pivideo/videos";

/// One slot entry as stored in config.json (managed by the web UI).
#[derive(Deserialize)]
struct SlotConfig {
    gpio: u8,
    video: Option<String>,
}

fn config_path() -> String {
    env::var("PIVIDEO_CONFIG").unwrap_or_else(|_| DEFAULT_CONFIG.to_string())
}

fn read_config() -> Result<HashMap<String, SlotConfig>> {
    let path = config_path();
    let content = fs::read_to_string(&path)
        .with_context(|| format!("Cannot read config: {}", path))?;
    serde_json::from_str(&content).context("Invalid config JSON")
}

/// On button press: look up the current video path for a slot (re-reads config
/// each time so reassignments via the web UI take effect immediately).
fn video_for_slot(slot: &str) -> Option<String> {
    let video_dir = env::var("PIVIDEO_VIDEO_DIR")
        .unwrap_or_else(|_| DEFAULT_VIDEO_DIR.to_string());
    let config = read_config().ok()?;
    let file = config.get(slot)?.video.as_ref()?;
    Some(format!("{}/{}", video_dir, file))
}

fn play_video(path: &str) -> Result<()> {
    log::info!("Playing: {}", path);
    Command::new("mpv")
        .args(["--fullscreen", "--no-terminal", path])
        .spawn()?
        .wait()?;
    Ok(())
}

fn main() -> Result<()> {
    env_logger::init();

    // Read GPIO pin assignments from config at startup.
    // The web UI manages this file; restart the daemon if the pin mapping changes.
    let config = read_config()?;
    let pin_to_slot: HashMap<u8, String> = config
        .iter()
        .map(|(slot, cfg)| (cfg.gpio, slot.clone()))
        .collect();

    if pin_to_slot.is_empty() {
        anyhow::bail!("No slots found in config.json");
    }

    let gpio = Gpio::new()?;
    let pins: Vec<_> = pin_to_slot
        .keys()
        .map(|&n| gpio.get(n).map(|p| (n, p.into_input_pullup())))
        .collect::<std::result::Result<_, _>>()?;

    log::info!("PiVideo daemon started, watching {} pins", pins.len());

    loop {
        for (pin_num, pin) in &pins {
            if pin.is_low() {
                let slot = &pin_to_slot[pin_num];
                match video_for_slot(slot) {
                    None => log::warn!("Slot {slot} has no video assigned"),
                    Some(path) => {
                        if let Err(e) = play_video(&path) {
                            log::error!("Playback error: {e}");
                        }
                    }
                }
                // Debounce: wait for button release
                while pin.is_low() {
                    std::thread::sleep(std::time::Duration::from_millis(50));
                }
            }
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
}
