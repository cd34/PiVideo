use anyhow::{Context, Result};
use rppal::gpio::Gpio;
use serde::Deserialize;
use std::collections::HashMap;
use std::process::{Child, Command};
use std::{env, fs, thread, time::Duration};

const DEFAULT_CONFIG: &str = "/opt/pivideo/config.json";
const DEFAULT_VIDEO_DIR: &str = "/opt/pivideo/videos";

#[derive(Deserialize, Default)]
struct SplashConfig {
    image: Option<String>,
    video: Option<String>,
}

#[derive(Deserialize)]
struct SlotConfig {
    gpio: u8,
    video: Option<String>,
}

fn config_path() -> String {
    env::var("PIVIDEO_CONFIG").unwrap_or_else(|_| DEFAULT_CONFIG.to_string())
}

fn video_dir() -> String {
    env::var("PIVIDEO_VIDEO_DIR").unwrap_or_else(|_| DEFAULT_VIDEO_DIR.to_string())
}

/// Read slot configs from config.json (gpio pin → video assignments).
/// Called at startup for pin setup, and on each button press for current video.
fn read_slots() -> Result<HashMap<String, SlotConfig>> {
    let path = config_path();
    let content = fs::read_to_string(&path)
        .with_context(|| format!("Cannot read config: {}", path))?;
    let raw: serde_json::Value = serde_json::from_str(&content)?;
    Ok(raw.as_object()
        .map(|obj| obj.iter()
            .filter(|(k, _)| k.parse::<u8>().is_ok())
            .filter_map(|(k, v)| {
                serde_json::from_value::<SlotConfig>(v.clone())
                    .ok()
                    .map(|cfg| (k.clone(), cfg))
            })
            .collect())
        .unwrap_or_default())
}

/// Read splash config. Called when entering idle state so web UI changes
/// to the splash image/video take effect without restarting the daemon.
fn read_splash() -> SplashConfig {
    let path = config_path();
    fs::read_to_string(path).ok()
        .and_then(|c| serde_json::from_str::<serde_json::Value>(&c).ok())
        .and_then(|v| v.get("splash").cloned())
        .and_then(|v| serde_json::from_value(v).ok())
        .unwrap_or_default()
}

/// Start the idle splash — looping video takes priority over still image.
/// Returns None if no splash is configured (screen goes blank/black).
fn start_idle() -> Option<Child> {
    let splash = read_splash();
    let dir = video_dir();
    if let Some(ref v) = splash.video {
        let path = format!("{}/{}", dir, v);
        log::info!("Idle: looping splash video {}", path);
        Command::new("mpv")
            .args(["--fullscreen", "--no-terminal", "--loop=inf", &path])
            .spawn().ok()
    } else if let Some(ref img) = splash.image {
        let path = format!("{}/{}", dir, img);
        log::info!("Idle: showing splash image {}", path);
        Command::new("mpv")
            .args(["--fullscreen", "--no-terminal",
                   "--loop=inf", "--image-display-duration=inf", &path])
            .spawn().ok()
    } else {
        log::info!("Idle: no splash configured");
        None
    }
}

/// Kill a running mpv child and reap it to avoid zombies.
fn kill_child(child: &mut Option<Child>) {
    if let Some(ref mut c) = child {
        let _ = c.kill();
        let _ = c.wait();
    }
    *child = None;
}

/// Non-blocking check: has the child process exited?
fn has_exited(child: &mut Option<Child>) -> bool {
    child.as_mut()
        .map(|c| matches!(c.try_wait(), Ok(Some(_))))
        .unwrap_or(false)
}

fn main() -> Result<()> {
    env_logger::init();

    // Read GPIO pin assignments at startup. The pin mapping is hardware-fixed
    // so we only read it once. Restart the daemon if pins change.
    let slots = read_slots()?;
    let pin_to_slot: HashMap<u8, String> = slots.iter()
        .map(|(slot, cfg)| (cfg.gpio, slot.clone()))
        .collect();

    if pin_to_slot.is_empty() {
        anyhow::bail!("No slots configured in config.json");
    }

    let gpio = Gpio::new()?;
    let pins: Vec<_> = pin_to_slot.keys()
        .map(|&n| gpio.get(n).map(|p| (n, p.into_input_pullup())))
        .collect::<std::result::Result<_, _>>()?;

    log::info!("PiVideo daemon started, watching {} pins", pins.len());

    // State machine: idle (showing splash) ↔ playing (button video)
    let mut current: Option<Child> = None;
    let mut idle = true;
    let mut idle_started = false; // prevents re-calling start_idle every 10ms

    loop {
        if idle {
            // Start or restart idle splash (also handles mpv crash recovery)
            if !idle_started || has_exited(&mut current) {
                kill_child(&mut current);
                current = start_idle();
                idle_started = true;
            }
        } else {
            // Playing a button video — check if it has finished
            if has_exited(&mut current) || current.is_none() {
                kill_child(&mut current);
                idle = true;
                idle_started = false; // will trigger start_idle on next tick
            }
        }

        // Check GPIO pins for button presses
        for (pin_num, pin) in &pins {
            if pin.is_low() {
                let slot = &pin_to_slot[pin_num];

                // Re-read video assignment so web UI changes take effect immediately
                let video_path = read_slots().ok()
                    .and_then(|s| s.get(slot)?.video.clone())
                    .map(|file| format!("{}/{}", video_dir(), file));

                match video_path {
                    None => log::warn!("Slot {slot} has no video assigned"),
                    Some(path) => {
                        // Interrupt any current video or idle splash
                        kill_child(&mut current);
                        idle = false;
                        idle_started = false;
                        log::info!("Playing: {}", path);
                        current = Command::new("mpv")
                            .args(["--fullscreen", "--no-terminal", &path])
                            .spawn().ok();
                    }
                }

                // Debounce: wait for button release before resuming
                while pin.is_low() {
                    thread::sleep(Duration::from_millis(50));
                }
            }
        }

        thread::sleep(Duration::from_millis(10));
    }
}
