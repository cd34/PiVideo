use anyhow::{Context, Result};
use rppal::gpio::Gpio;
use serde::Deserialize;
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::process::{Child, Command};
use std::{env, fs, thread, time::Duration};

const DEFAULT_CONFIG: &str = "/opt/pivideo/config.json";
const DEFAULT_VIDEO_DIR: &str = "/opt/pivideo/videos";
const MPV_SOCKET: &str = "/tmp/pivideo-mpv.sock";

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

/// Read slot configs from a config file at `path`.
fn read_slots_from(path: &str) -> Result<HashMap<String, SlotConfig>> {
    let content = fs::read_to_string(path)
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

/// Read slot configs from config.json (gpio pin → video assignments).
/// Called at startup for pin setup, and on each button press for current video.
fn read_slots() -> Result<HashMap<String, SlotConfig>> {
    read_slots_from(&config_path())
}

/// Read splash config from a config file at `path`.
fn read_splash_from(path: &str) -> SplashConfig {
    fs::read_to_string(path).ok()
        .and_then(|c| serde_json::from_str::<serde_json::Value>(&c).ok())
        .and_then(|v| v.get("splash").cloned())
        .and_then(|v| serde_json::from_value(v).ok())
        .unwrap_or_default()
}

/// Read splash config. Called when entering idle state so web UI changes
/// to the splash image/video take effect without restarting the daemon.
fn read_splash() -> SplashConfig {
    read_splash_from(&config_path())
}

/// Resolve the splash config to a file path (video takes priority over image).
fn splash_path(splash: &SplashConfig) -> Option<String> {
    let dir = video_dir();
    splash.video.as_ref()
        .or(splash.image.as_ref())
        .map(|f| format!("{}/{}", dir, f))
}

// ── Persistent mpv instance with IPC control ────────────────────────────────

struct Mpv {
    child: Child,
    writer: UnixStream,
    reader: BufReader<UnixStream>,
    line_buf: String,
}

impl Mpv {
    /// Spawn mpv in idle mode with an IPC socket for seamless video switching.
    fn spawn() -> Result<Self> {
        let _ = fs::remove_file(MPV_SOCKET);

        let child = Command::new("mpv")
            .args([
                "--fullscreen",
                "--no-terminal",
                "--idle",
                "--image-display-duration=inf",
                &format!("--input-ipc-server={}", MPV_SOCKET),
            ])
            .spawn()
            .context("Failed to start mpv")?;

        // Wait for the IPC socket to appear
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        while std::time::Instant::now() < deadline {
            if std::path::Path::new(MPV_SOCKET).exists() {
                break;
            }
            thread::sleep(Duration::from_millis(50));
        }

        let writer = UnixStream::connect(MPV_SOCKET)
            .context("Failed to connect to mpv IPC socket")?;
        let reader_stream = writer.try_clone()?;
        reader_stream.set_nonblocking(true)?;

        let mut mpv = Mpv {
            child,
            writer,
            reader: BufReader::new(reader_stream),
            line_buf: String::new(),
        };

        // Observe the idle-active property so we know when playback finishes
        mpv.send(&serde_json::json!({"command": ["observe_property", 1, "idle-active"]}))?;

        Ok(mpv)
    }

    /// Send a JSON command to mpv (fire-and-forget).
    fn send(&mut self, cmd: &serde_json::Value) -> Result<()> {
        writeln!(self.writer, "{}", cmd)?;
        Ok(())
    }

    /// Load a file. If `looping` is true, the file loops forever (for splash).
    fn load_file(&mut self, path: &str, looping: bool) {
        let loop_val = if looping { "inf" } else { "no" };
        let _ = self.send(&serde_json::json!({"command": ["set_property", "loop-file", loop_val]}));
        let _ = self.send(&serde_json::json!({"command": ["loadfile", path, "replace"]}));
    }

    /// Non-blocking check: did mpv become idle (i.e. playback finished)?
    /// Drains all pending events and returns true if idle-active became true.
    fn poll_idle(&mut self) -> bool {
        let mut became_idle = false;
        loop {
            self.line_buf.clear();
            match self.reader.read_line(&mut self.line_buf) {
                Ok(0) => break,  // EOF
                Ok(_) => {
                    if let Ok(v) = serde_json::from_str::<serde_json::Value>(&self.line_buf) {
                        if v.get("event").and_then(|e| e.as_str()) == Some("property-change")
                            && v.get("id") == Some(&serde_json::json!(1))
                            && v.get("data") == Some(&serde_json::json!(true))
                        {
                            became_idle = true;
                        }
                    }
                }
                Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(_) => break,
            }
        }
        became_idle
    }

    fn is_alive(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }

    fn kill(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Drop for Mpv {
    fn drop(&mut self) {
        self.kill();
        let _ = fs::remove_file(MPV_SOCKET);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static COUNTER: AtomicU64 = AtomicU64::new(0);

    /// RAII temp file — deleted when dropped.
    struct TempConfig(std::path::PathBuf);

    impl TempConfig {
        fn new(content: &str) -> Self {
            let n = COUNTER.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir().join(format!("pivideo_test_{}.json", n));
            std::fs::write(&path, content).unwrap();
            TempConfig(path)
        }
        fn path(&self) -> &str { self.0.to_str().unwrap() }
    }

    impl Drop for TempConfig {
        fn drop(&mut self) { let _ = std::fs::remove_file(&self.0); }
    }

    const FULL_CONFIG: &str = r#"{
        "1": {"gpio": 4,  "pin": 7,  "video": "intro.mp4"},
        "2": {"gpio": 17, "pin": 11, "video": null},
        "3": {"gpio": 22, "pin": 15, "video": null},
        "4": {"gpio": 23, "pin": 16, "video": null},
        "5": {"gpio": 24, "pin": 18, "video": null},
        "6": {"gpio": 25, "pin": 22, "video": null},
        "7": {"gpio": 27, "pin": 13, "video": null},
        "splash": {"image": "bg.jpg", "video": "loop.mp4"}
    }"#;

    // ── read_slots_from ──────────────────────────────────────────────────────

    #[test]
    fn slots_parses_all_seven() {
        let f = TempConfig::new(FULL_CONFIG);
        let slots = read_slots_from(f.path()).unwrap();
        assert_eq!(slots.len(), 7);
    }

    #[test]
    fn slots_parses_gpio_and_video() {
        let f = TempConfig::new(FULL_CONFIG);
        let slots = read_slots_from(f.path()).unwrap();
        let s1 = slots.get("1").unwrap();
        assert_eq!(s1.gpio, 4);
        assert_eq!(s1.video.as_deref(), Some("intro.mp4"));
    }

    #[test]
    fn slots_null_video_is_none() {
        let f = TempConfig::new(FULL_CONFIG);
        let slots = read_slots_from(f.path()).unwrap();
        assert!(slots.get("2").unwrap().video.is_none());
    }

    #[test]
    fn slots_ignores_non_numeric_keys() {
        // "splash" key must not appear as a slot
        let f = TempConfig::new(FULL_CONFIG);
        let slots = read_slots_from(f.path()).unwrap();
        assert!(!slots.contains_key("splash"));
    }

    #[test]
    fn slots_missing_file_is_err() {
        assert!(read_slots_from("/tmp/pivideo_no_such_file_xyz.json").is_err());
    }

    #[test]
    fn slots_invalid_json_is_err() {
        let f = TempConfig::new("not json {{{");
        assert!(read_slots_from(f.path()).is_err());
    }

    #[test]
    fn slots_skips_entry_missing_gpio() {
        // A slot without the required "gpio" field is silently dropped
        let f = TempConfig::new(r#"{"1": {"video": "clip.mp4"}, "2": {"gpio": 17, "video": null}}"#);
        let slots = read_slots_from(f.path()).unwrap();
        assert!(!slots.contains_key("1"), "malformed slot should be skipped");
        assert!(slots.contains_key("2"));
    }

    #[test]
    fn slots_empty_config_is_ok() {
        let f = TempConfig::new("{}");
        let slots = read_slots_from(f.path()).unwrap();
        assert!(slots.is_empty());
    }

    // ── read_splash_from ─────────────────────────────────────────────────────

    #[test]
    fn splash_parses_image_and_video() {
        let f = TempConfig::new(FULL_CONFIG);
        let s = read_splash_from(f.path());
        assert_eq!(s.image.as_deref(), Some("bg.jpg"));
        assert_eq!(s.video.as_deref(), Some("loop.mp4"));
    }

    #[test]
    fn splash_null_fields_are_none() {
        let f = TempConfig::new(r#"{"splash": {"image": null, "video": null}}"#);
        let s = read_splash_from(f.path());
        assert!(s.image.is_none());
        assert!(s.video.is_none());
    }

    #[test]
    fn splash_missing_key_returns_default() {
        let f = TempConfig::new(r#"{"1": {"gpio": 4, "video": null}}"#);
        let s = read_splash_from(f.path());
        assert!(s.image.is_none());
        assert!(s.video.is_none());
    }

    #[test]
    fn splash_missing_file_returns_default() {
        let s = read_splash_from("/tmp/pivideo_no_such_file_xyz.json");
        assert!(s.image.is_none());
        assert!(s.video.is_none());
    }

    #[test]
    fn splash_invalid_json_returns_default() {
        let f = TempConfig::new("not json");
        let s = read_splash_from(f.path());
        assert!(s.image.is_none());
        assert!(s.video.is_none());
    }

    // ── splash_path ─────────────────────────────────────────────────────────

    #[test]
    fn splash_path_prefers_video() {
        let s = SplashConfig {
            image: Some("bg.jpg".into()),
            video: Some("loop.mp4".into()),
        };
        let p = splash_path(&s).unwrap();
        assert!(p.ends_with("loop.mp4"));
    }

    #[test]
    fn splash_path_falls_back_to_image() {
        let s = SplashConfig { image: Some("bg.jpg".into()), video: None };
        let p = splash_path(&s).unwrap();
        assert!(p.ends_with("bg.jpg"));
    }

    #[test]
    fn splash_path_none_when_empty() {
        let s = SplashConfig { image: None, video: None };
        assert!(splash_path(&s).is_none());
    }

    // ── Concurrent button press behaviour ────────────────────────────────────
    //
    // The daemon uses a single-threaded blocking poll loop. When multiple pins
    // go low simultaneously they are handled in iteration order:
    //
    //   1. First low pin found → video loaded via IPC, debounce loop blocks on
    //      that pin until it goes HIGH again.
    //   2. All other pins are ignored while the debounce loop is running.
    //   3. After pin A is released, the outer loop resumes. If pin B is still
    //      low it is handled next, loading a new video that replaces A's.
    //
    // Consequence: holding button A blocks detection of button B until A is
    // released. This is intentional kiosk behaviour — a single physical press
    // maps unambiguously to one video. GPIO hardware is required to test this
    // path directly, so it is covered by code-review rather than unit tests.
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

    let mut mpv = Mpv::spawn()?;
    let mut idle = true;

    // Start idle splash immediately
    let splash = read_splash();
    if let Some(path) = splash_path(&splash) {
        log::info!("Idle: {}", path);
        mpv.load_file(&path, true);
    }

    loop {
        if !mpv.is_alive() {
            log::warn!("mpv crashed, restarting...");
            mpv = Mpv::spawn()?;
            idle = true;
            let splash = read_splash();
            if let Some(path) = splash_path(&splash) {
                log::info!("Idle: {}", path);
                mpv.load_file(&path, true);
            }
        }

        if idle {
            // Drain events while idle (nothing to act on)
            let _ = mpv.poll_idle();
        } else {
            // Playing a button video — return to idle when it finishes
            if mpv.poll_idle() {
                idle = true;
                let splash = read_splash();
                if let Some(path) = splash_path(&splash) {
                    log::info!("Idle: {}", path);
                    mpv.load_file(&path, true);
                }
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
                        log::info!("Playing: {}", path);
                        mpv.load_file(&path, false);
                        idle = false;
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
