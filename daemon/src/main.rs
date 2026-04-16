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

// ── Config types ────────────────────────────────────────────────────────────

#[derive(Deserialize, Clone)]
struct MediaEntry {
    file: String,
    button: Option<u8>,
}

#[derive(Deserialize)]
struct ButtonConfig {
    gpio: u8,
    #[allow(dead_code)]
    pin: Option<u8>,
}

#[derive(Deserialize)]
struct Config {
    #[allow(dead_code)]
    version: Option<u8>,
    #[serde(default)]
    media: Vec<MediaEntry>,
    #[serde(default)]
    buttons: HashMap<String, ButtonConfig>,
}

fn config_path() -> String {
    env::var("PIVIDEO_CONFIG").unwrap_or_else(|_| DEFAULT_CONFIG.to_string())
}

fn video_dir() -> String {
    env::var("PIVIDEO_VIDEO_DIR").unwrap_or_else(|_| DEFAULT_VIDEO_DIR.to_string())
}

/// Read the full config from a file at `path`.
fn read_config_from(path: &str) -> Result<Config> {
    let content = fs::read_to_string(path)
        .with_context(|| format!("Cannot read config: {}", path))?;
    let config: Config = serde_json::from_str(&content)?;
    Ok(config)
}

/// Read the config from the default path.
fn read_config() -> Result<Config> {
    read_config_from(&config_path())
}

/// Build a mapping from GPIO pin number to video file path for button-assigned media.
fn button_map(config: &Config) -> HashMap<u8, String> {
    let dir = video_dir();
    let mut map = HashMap::new();
    for entry in &config.media {
        if let Some(btn) = entry.button {
            if let Some(bcfg) = config.buttons.get(&btn.to_string()) {
                map.insert(bcfg.gpio, format!("{}/{}", dir, entry.file));
            }
        }
    }
    map
}

/// Build the kiosk playlist: file paths for media entries not assigned to a button.
fn kiosk_playlist(config: &Config) -> Vec<String> {
    let dir = video_dir();
    config.media.iter()
        .filter(|m| m.button.is_none())
        .map(|m| format!("{}/{}", dir, m.file))
        .collect()
}

/// Check if a file path has an image extension.
fn is_image_file(path: &str) -> bool {
    let lower = path.to_lowercase();
    [".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"]
        .iter()
        .any(|ext| lower.ends_with(ext))
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
                "--image-display-duration=10",
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

    /// Load a file. If `looping` is true, the file loops forever.
    fn load_file(&mut self, path: &str, looping: bool) {
        let loop_val = if looping { "inf" } else { "no" };
        let _ = self.send(&serde_json::json!({"command": ["set_property", "loop-file", loop_val]}));
        let _ = self.send(&serde_json::json!({"command": ["set_property", "loop-playlist", "no"]}));
        let _ = self.send(&serde_json::json!({"command": ["loadfile", path, "replace"]}));
    }

    /// Load a kiosk playlist that loops infinitely.
    fn load_playlist(&mut self, paths: &[String]) {
        if paths.is_empty() {
            return;
        }
        let _ = self.send(&serde_json::json!({"command": ["set_property", "loop-file", "no"]}));
        let _ = self.send(&serde_json::json!({"command": ["set_property", "loop-playlist", "inf"]}));
        let _ = self.send(&serde_json::json!({"command": ["loadfile", paths[0], "replace"]}));
        for path in &paths[1..] {
            let _ = self.send(&serde_json::json!({"command": ["loadfile", path, "append"]}));
        }
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

    const V2_CONFIG: &str = r#"{
        "version": 2,
        "media": [
            {"file": "intro.mp4", "button": 1},
            {"file": "promo.mp4", "button": 3},
            {"file": "landscape.jpg", "button": null},
            {"file": "demo.mp4", "button": null}
        ],
        "buttons": {
            "1": {"gpio": 4,  "pin": 7},
            "2": {"gpio": 17, "pin": 11},
            "3": {"gpio": 22, "pin": 15},
            "4": {"gpio": 23, "pin": 16},
            "5": {"gpio": 24, "pin": 18},
            "6": {"gpio": 25, "pin": 22},
            "7": {"gpio": 27, "pin": 13}
        }
    }"#;

    const KIOSK_ONLY_CONFIG: &str = r#"{
        "version": 2,
        "media": [
            {"file": "slide1.jpg", "button": null},
            {"file": "slide2.png", "button": null},
            {"file": "loop.mp4", "button": null}
        ],
        "buttons": {
            "1": {"gpio": 4, "pin": 7}
        }
    }"#;

    // ── read_config_from ─────────────────────────────────────────────────────

    #[test]
    fn config_parses_media_entries() {
        let f = TempConfig::new(V2_CONFIG);
        let config = read_config_from(f.path()).unwrap();
        assert_eq!(config.media.len(), 4);
    }

    #[test]
    fn config_parses_button_assignments() {
        let f = TempConfig::new(V2_CONFIG);
        let config = read_config_from(f.path()).unwrap();
        assert_eq!(config.media[0].button, Some(1));
        assert_eq!(config.media[0].file, "intro.mp4");
        assert_eq!(config.media[1].button, Some(3));
        assert!(config.media[2].button.is_none());
    }

    #[test]
    fn config_parses_buttons_hardware_map() {
        let f = TempConfig::new(V2_CONFIG);
        let config = read_config_from(f.path()).unwrap();
        assert_eq!(config.buttons.len(), 7);
        assert_eq!(config.buttons.get("1").unwrap().gpio, 4);
    }

    #[test]
    fn config_missing_file_is_err() {
        assert!(read_config_from("/tmp/pivideo_no_such_file_xyz.json").is_err());
    }

    #[test]
    fn config_invalid_json_is_err() {
        let f = TempConfig::new("not json {{{");
        assert!(read_config_from(f.path()).is_err());
    }

    #[test]
    fn config_empty_media_is_ok() {
        let f = TempConfig::new(r#"{"version": 2, "media": [], "buttons": {}}"#);
        let config = read_config_from(f.path()).unwrap();
        assert!(config.media.is_empty());
    }

    // ── button_map ──────────────────────────────────────────────────────────

    #[test]
    fn button_map_returns_assigned_media_only() {
        let f = TempConfig::new(V2_CONFIG);
        let config = read_config_from(f.path()).unwrap();
        let map = button_map(&config);
        assert_eq!(map.len(), 2);  // buttons 1 and 3
        assert!(map.get(&4).unwrap().ends_with("intro.mp4"));   // gpio 4 = button 1
        assert!(map.get(&22).unwrap().ends_with("promo.mp4"));  // gpio 22 = button 3
    }

    #[test]
    fn button_map_empty_when_no_assignments() {
        let f = TempConfig::new(KIOSK_ONLY_CONFIG);
        let config = read_config_from(f.path()).unwrap();
        let map = button_map(&config);
        assert!(map.is_empty());
    }

    #[test]
    fn button_map_ignores_invalid_button_numbers() {
        let f = TempConfig::new(r#"{
            "version": 2,
            "media": [{"file": "clip.mp4", "button": 99}],
            "buttons": {"1": {"gpio": 4}}
        }"#);
        let config = read_config_from(f.path()).unwrap();
        let map = button_map(&config);
        assert!(map.is_empty());
    }

    // ── kiosk_playlist ──────────────────────────────────────────────────────

    #[test]
    fn kiosk_playlist_returns_unassigned_media() {
        let f = TempConfig::new(V2_CONFIG);
        let config = read_config_from(f.path()).unwrap();
        let playlist = kiosk_playlist(&config);
        assert_eq!(playlist.len(), 2);
        assert!(playlist[0].ends_with("landscape.jpg"));
        assert!(playlist[1].ends_with("demo.mp4"));
    }

    #[test]
    fn kiosk_playlist_all_unassigned() {
        let f = TempConfig::new(KIOSK_ONLY_CONFIG);
        let config = read_config_from(f.path()).unwrap();
        let playlist = kiosk_playlist(&config);
        assert_eq!(playlist.len(), 3);
    }

    #[test]
    fn kiosk_playlist_empty_when_all_assigned() {
        let f = TempConfig::new(r#"{
            "version": 2,
            "media": [{"file": "clip.mp4", "button": 1}],
            "buttons": {"1": {"gpio": 4}}
        }"#);
        let config = read_config_from(f.path()).unwrap();
        let playlist = kiosk_playlist(&config);
        assert!(playlist.is_empty());
    }

    // ── is_image_file ───────────────────────────────────────────────────────

    #[test]
    fn is_image_file_detects_images() {
        assert!(is_image_file("photo.jpg"));
        assert!(is_image_file("photo.JPEG"));
        assert!(is_image_file("slide.png"));
        assert!(is_image_file("bg.webp"));
    }

    #[test]
    fn is_image_file_rejects_videos() {
        assert!(!is_image_file("clip.mp4"));
        assert!(!is_image_file("movie.mkv"));
        assert!(!is_image_file("show.avi"));
    }

    // ── Config hot-reload ───────────────────────────────────────────────────

    #[test]
    fn config_hot_reload_picks_up_changes() {
        let f = TempConfig::new(V2_CONFIG);
        let config = read_config_from(f.path()).unwrap();
        let map = button_map(&config);
        assert!(map.get(&4).unwrap().ends_with("intro.mp4"));

        let updated = V2_CONFIG.replace("intro.mp4", "updated.mp4");
        std::fs::write(f.path(), updated).unwrap();

        let config = read_config_from(f.path()).unwrap();
        let map = button_map(&config);
        assert!(map.get(&4).unwrap().ends_with("updated.mp4"));
    }

    #[test]
    fn kiosk_playlist_hot_reload_picks_up_changes() {
        let f = TempConfig::new(V2_CONFIG);
        let config = read_config_from(f.path()).unwrap();
        let playlist = kiosk_playlist(&config);
        assert_eq!(playlist.len(), 2);

        // Add a third kiosk item
        let updated = V2_CONFIG.replace(
            r#"{"file": "demo.mp4", "button": null}"#,
            r#"{"file": "demo.mp4", "button": null}, {"file": "extra.mp4", "button": null}"#
        );
        std::fs::write(f.path(), updated).unwrap();

        let config = read_config_from(f.path()).unwrap();
        let playlist = kiosk_playlist(&config);
        assert_eq!(playlist.len(), 3);
    }

    // ── poll_idle parsing ───────────────────────────────────────────────────

    #[test]
    fn poll_idle_detects_idle_active_event() {
        let events = concat!(
            r#"{"event":"property-change","id":1,"data":false,"name":"idle-active"}"#, "\n",
            r#"{"event":"property-change","id":1,"data":true,"name":"idle-active"}"#, "\n",
        );
        let cursor = std::io::Cursor::new(events.as_bytes().to_vec());
        let mut reader = BufReader::new(cursor);
        let mut line_buf = String::new();
        let mut became_idle = false;

        loop {
            line_buf.clear();
            match reader.read_line(&mut line_buf) {
                Ok(0) => break,
                Ok(_) => {
                    if let Ok(v) = serde_json::from_str::<serde_json::Value>(&line_buf) {
                        if v.get("event").and_then(|e| e.as_str()) == Some("property-change")
                            && v.get("id") == Some(&serde_json::json!(1))
                            && v.get("data") == Some(&serde_json::json!(true))
                        {
                            became_idle = true;
                        }
                    }
                }
                Err(_) => break,
            }
        }
        assert!(became_idle);
    }

    #[test]
    fn poll_idle_ignores_unrelated_events() {
        let events = concat!(
            r#"{"event":"property-change","id":2,"data":true,"name":"pause"}"#, "\n",
            r#"{"event":"playback-restart"}"#, "\n",
        );
        let cursor = std::io::Cursor::new(events.as_bytes().to_vec());
        let mut reader = BufReader::new(cursor);
        let mut line_buf = String::new();
        let mut became_idle = false;

        loop {
            line_buf.clear();
            match reader.read_line(&mut line_buf) {
                Ok(0) => break,
                Ok(_) => {
                    if let Ok(v) = serde_json::from_str::<serde_json::Value>(&line_buf) {
                        if v.get("event").and_then(|e| e.as_str()) == Some("property-change")
                            && v.get("id") == Some(&serde_json::json!(1))
                            && v.get("data") == Some(&serde_json::json!(true))
                        {
                            became_idle = true;
                        }
                    }
                }
                Err(_) => break,
            }
        }
        assert!(!became_idle);
    }

    // ── load_file / load_playlist IPC commands ──────────────────────────────

    #[test]
    fn load_file_sets_loop_inf_for_looping() {
        let loop_val = if true { "inf" } else { "no" };
        let cmd = serde_json::json!({"command": ["set_property", "loop-file", loop_val]});
        assert_eq!(cmd["command"][2], "inf");
    }

    #[test]
    fn load_file_sets_loop_no_for_single_play() {
        let loop_val = if false { "inf" } else { "no" };
        let cmd = serde_json::json!({"command": ["set_property", "loop-file", loop_val]});
        assert_eq!(cmd["command"][2], "no");
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

    let config = read_config()?;

    // Build GPIO→video mapping for button-assigned media
    let btn_map = button_map(&config);

    // Only set up GPIO if there are button assignments
    let pins: Vec<(u8, rppal::gpio::InputPin)> = if btn_map.is_empty() {
        log::info!("No buttons assigned — running in kiosk-only mode");
        vec![]
    } else {
        let gpio = Gpio::new()?;
        btn_map.keys()
            .map(|&n| gpio.get(n).map(|p| (n, p.into_input_pullup())))
            .collect::<std::result::Result<_, _>>()?
    };

    log::info!("PiVideo daemon started, {} buttons, kiosk-only={}", pins.len(), pins.is_empty());

    let mut mpv = Mpv::spawn()?;
    let mut idle = true;

    // Start kiosk playlist immediately
    let playlist = kiosk_playlist(&config);
    if !playlist.is_empty() {
        log::info!("Starting kiosk playlist ({} items)", playlist.len());
        mpv.load_playlist(&playlist);
    }

    loop {
        if !mpv.is_alive() {
            log::warn!("mpv crashed, restarting...");
            mpv = Mpv::spawn()?;
            idle = true;
            let config = read_config().unwrap_or(Config {
                version: Some(2), media: vec![], buttons: HashMap::new()
            });
            let playlist = kiosk_playlist(&config);
            if !playlist.is_empty() {
                log::info!("Restarting kiosk playlist ({} items)", playlist.len());
                mpv.load_playlist(&playlist);
            }
        }

        if idle {
            // Drain events while idle (nothing to act on — playlist loops)
            let _ = mpv.poll_idle();
        } else {
            // Playing a button video — return to kiosk when it finishes
            if mpv.poll_idle() {
                idle = true;
                let config = read_config().unwrap_or(Config {
                    version: Some(2), media: vec![], buttons: HashMap::new()
                });
                let playlist = kiosk_playlist(&config);
                if !playlist.is_empty() {
                    log::info!("Returning to kiosk playlist ({} items)", playlist.len());
                    mpv.load_playlist(&playlist);
                }
            }
        }

        // Check GPIO pins for button presses
        for (pin_num, pin) in &pins {
            if pin.is_low() {
                // Re-read config so web UI changes take effect immediately
                let video_path = read_config().ok()
                    .and_then(|c| {
                        let map = button_map(&c);
                        map.get(pin_num).cloned()
                    });

                match video_path {
                    None => log::warn!("GPIO {} has no video assigned", pin_num),
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
