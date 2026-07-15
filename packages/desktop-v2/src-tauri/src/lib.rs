// Tauri 2.0 application library.
//
// Spawns the bundled `nexus-server` PyInstaller binary as a sidecar
// on startup so the medic doesn't have to launch the backend
// separately. The sidecar is registered in tauri.conf.json's
// `externalBin` array.
//
// If the sidecar dies, the frontend keeps running — login will fail
// fast and show "Cannot reach server. Is the backend running?" so
// the medic can restart the app.

use std::collections::{HashMap, VecDeque};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, RunEvent, State};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};

// ── Server-mode configuration ──────────────────────────────────────────────
//
// Persisted at $RUNE_HOME/server_config.json. Two modes:
//   "local"  — spawn the bundled sidecar (original behaviour)
//   "remote" — skip the sidecar; the frontend connects to remote_url instead
//
// This file is optional: missing = "local" (backwards-compatible default).

#[derive(Serialize, Deserialize, Clone)]
struct ServerConfigFile {
    /// "local" | "remote"
    mode: String,
    /// Required when mode == "remote". Must be a valid https:// URL.
    remote_url: Option<String>,
}

impl Default for ServerConfigFile {
    fn default() -> Self {
        Self { mode: "local".to_string(), remote_url: None }
    }
}

fn server_config_path() -> PathBuf {
    rune_home().join("server_config.json")
}

fn read_server_config() -> ServerConfigFile {
    let path = server_config_path();
    if let Ok(data) = fs::read_to_string(&path) {
        if let Ok(cfg) = serde_json::from_str::<ServerConfigFile>(&data) {
            return cfg;
        }
    }
    ServerConfigFile::default()
}

fn write_server_config(cfg: ServerConfigFile) -> Result<(), String> {
    let path = server_config_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("mkdir {}: {e}", parent.display()))?;
    }
    let json = serde_json::to_string_pretty(&cfg)
        .map_err(|e| format!("serialise: {e}"))?;
    // Atomic write via tempfile so a crash mid-write can't corrupt the config.
    let tmp = path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join("server_config.json.tmp");
    fs::write(&tmp, json)
        .map_err(|e| format!("write {}: {e}", tmp.display()))?;
    fs::rename(&tmp, &path)
        .map_err(|e| format!("rename {}: {e}", path.display()))
}

/// Holds a handle to the running sidecar so we can shut it down
/// cleanly on app exit. None until startup has spawned it.
struct SidecarState(Mutex<Option<CommandChild>>);

/// One captured line of sidecar stdout/stderr, kept in memory so the
/// frontend can ask for the tail at any time (including from the
/// LoginView's "Cannot reach server" diagnostic panel — without this,
/// a startup crash leaves the user with zero clue what went wrong).
#[derive(Clone, serde::Serialize)]
struct DiagLine {
    /// Unix-seconds wall-clock at capture time.
    ts: u64,
    /// "stdout" | "stderr" | "sys"  (sys = synthesized events like
    /// "spawn ok pid=...", "sidecar terminated code=...").
    stream: &'static str,
    text: String,
}

/// Diagnostics shared between the spawn task and the get_sidecar_diagnostics
/// IPC. Held behind an Arc<Mutex<>> so the async drain task and the IPC
/// can race on it without lifetime grief.
///
/// The struct also remembers the path of the dedicated sidecar log file —
/// frontends surface this string in the diagnostic panel so the user can
/// `tail -f` it from a terminal without guessing.
#[derive(Default)]
struct SidecarDiagInner {
    /// Last N captured lines (FIFO; ring buffer of 400 entries).
    buffer: VecDeque<DiagLine>,
    /// Most-recently-spawned child pid, or None before first spawn.
    pid: Option<u32>,
    /// Last sidecar exit code as reported by CommandEvent::Terminated.
    /// None means "still running" (or "never spawned"); look at `pid`
    /// to disambiguate.
    last_exit_code: Option<i32>,
    /// True between spawn() and the corresponding Terminated event.
    alive: bool,
    /// Unix-seconds at the last spawn() — distinguishes "just rebooted"
    /// from "been running for hours" in the diag panel.
    started_at: u64,
    /// Absolute path of the on-disk log file ($LOGDIR/Nexus/sidecar.log).
    log_path: PathBuf,
}

#[derive(Clone, Default)]
struct SidecarDiag(Arc<Mutex<SidecarDiagInner>>);

impl SidecarDiag {
    /// Max in-memory ring-buffer size. 400 lines is enough to catch a
    /// PyInstaller startup traceback (typically 30–80 lines) plus a
    /// minute of normal uvicorn output afterwards.
    const RING_CAP: usize = 400;

    fn push(&self, stream: &'static str, text: String) {
        let mut g = match self.0.lock() {
            Ok(g) => g,
            Err(p) => p.into_inner(), // poisoned — recover anyway
        };
        if g.buffer.len() == Self::RING_CAP {
            g.buffer.pop_front();
        }
        let line = DiagLine { ts: unix_now_secs_u64(), stream, text };
        // Also append to the on-disk log so users can `tail -f` it. We
        // intentionally write through every event — a small perf cost
        // we trade for the property "if Nexus crashed, the log is on
        // disk up to the last line we observed".
        if let Ok(mut f) = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&g.log_path)
        {
            let _ = writeln!(f, "{} [{}] {}", line.ts, line.stream, line.text);
        }
        g.buffer.push_back(line);
    }

    fn mark_spawned(&self, pid: u32) {
        let mut g = self.0.lock().unwrap();
        g.pid = Some(pid);
        g.alive = true;
        g.last_exit_code = None;
        g.started_at = unix_now_secs_u64();
    }

    fn mark_exited(&self, code: Option<i32>) {
        let mut g = self.0.lock().unwrap();
        g.alive = false;
        g.last_exit_code = code;
    }

    fn snapshot(&self) -> serde_json::Value {
        let g = self.0.lock().unwrap();
        serde_json::json!({
            "pid":            g.pid,
            "alive":          g.alive,
            "last_exit_code": g.last_exit_code,
            "started_at":     g.started_at,
            "log_path":       g.log_path.to_string_lossy(),
            // Newest-last; UI typically renders this top-down with the
            // newest line at the bottom (matches `tail -f` mental model).
            "lines":          g.buffer.iter().collect::<Vec<_>>(),
        })
    }
}

/// Build identity baked in at compile time by `scripts/build-macos.sh`
/// (which exports NEXUS_BUILD_ID before invoking `pnpm tauri:build`).
/// option_env! returns None if the var wasn't set (e.g. when someone
/// runs `cargo build` directly) — we fall back to "dev".
const BUILD_ID: &str = match option_env!("NEXUS_BUILD_ID") {
    Some(v) => v,
    None => "dev",
};

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        // log plugin writes logs to:
        //   stdout (visible when launched from terminal)
        //   ~/Library/Logs/<bundle-id>/<app-name>.log on macOS
        // This is critical for debugging sidecar startup failures —
        // without it, log::info / log::error from spawn_backend_sidecar
        // disappear into the void in a bundled .dmg.
        .plugin(
            tauri_plugin_log::Builder::default()
                .level(log::LevelFilter::Info)
                .targets([
                    tauri_plugin_log::Target::new(tauri_plugin_log::TargetKind::Stdout),
                    tauri_plugin_log::Target::new(tauri_plugin_log::TargetKind::LogDir {
                        file_name: Some("nexus".to_string()),
                    }),
                ])
                .build(),
        )
        .manage(SidecarState(Mutex::new(None)))
        .manage::<SidecarDiag>(SidecarDiag::default())
        .invoke_handler(tauri::generate_handler![
            server_health,
            llm_env_status,
            llm_env_write,
            restart_sidecar,
            get_sidecar_diagnostics,
            get_server_mode,
            set_server_mode,
        ])
        .setup(|app| {
            log::info!("Nexus desktop v{} starting", BUILD_ID);
            // Initialise the diag state with the resolved log path
            // BEFORE the first spawn, so push() has somewhere to write
            // even if the very first stdout chunk arrives mid-setup.
            let diag: State<SidecarDiag> = app.handle().state();
            let log_path = sidecar_log_path();
            // Best-effort: create parent dir + open the file once so
            // subsequent appends from push() succeed silently. Any
            // failure here just means the in-memory ring still works
            // — the panel will still show useful info.
            if let Some(parent) = log_path.parent() {
                let _ = fs::create_dir_all(parent);
            }
            {
                let mut g = diag.0.lock().unwrap();
                g.log_path = log_path.clone();
            }
            diag.push("sys", format!(
                "Nexus desktop v{} starting; sidecar log at {}",
                BUILD_ID, log_path.display(),
            ));
            let server_cfg = read_server_config();
            if server_cfg.mode == "remote" {
                let url = server_cfg.remote_url.as_deref().unwrap_or("(unconfigured)");
                log::info!("remote-server mode: connecting to {url}; skipping sidecar spawn");
                diag.push("sys", format!("remote-server mode — sidecar skipped; target: {url}"));
            } else {
                spawn_backend_sidecar(app.handle())?;
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            // Reap the sidecar on app exit so we don't orphan it.
            if let RunEvent::ExitRequested { .. } | RunEvent::Exit = event {
                let state: State<SidecarState> = app_handle.state();
                // Bind the lock result to a named local so its drop
                // order is well-defined relative to `state`. Anonymous
                // temporaries in the `if let` scrutinee can outlive
                // `state` in some compiler versions, which the borrow
                // checker rejects (E0597).
                let lock_result = state.0.lock();
                if let Ok(mut guard) = lock_result {
                    if let Some(child) = guard.take() {
                        log::info!("killing nexus-server sidecar (pid={})", child.pid());
                        let _ = child.kill();
                    }
                }
            }
        });
}

/// Resolve the user-level data directory where v1's setup.sh writes
/// `.env` (GEMINI_API_KEY, etc.). We share the same location with v1 so
/// a medic who already ran the v1 installer doesn't have to re-enter
/// keys — Settings · LLM in v2 reads and writes the same file.
fn rune_home() -> PathBuf {
    // RUNE_HOME = "$HOME/Library/Application Support/RuneProtocol" on
    // macOS — same path the legacy Avalonia installer wrote to (see git
    // tag legacy/avalonia-final), so existing users keep their .env.
    // On non-macOS we fall back to a portable XDG path so the same
    // logic works under `pnpm tauri:dev` on Linux.
    if cfg!(target_os = "macos") {
        if let Some(home) = dirs_home() {
            return home.join("Library").join("Application Support").join("RuneProtocol");
        }
    }
    if let Some(home) = dirs_home() {
        return home.join(".config").join("RuneProtocol");
    }
    PathBuf::from(".")
}

fn dirs_home() -> Option<PathBuf> {
    // Avoid an extra crate dep — read $HOME (set on macOS + Linux) or
    // $USERPROFILE (Windows). Tauri's path API would also work but
    // requires &App which we don't have at this call site.
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
}

/// Where we persist raw sidecar stdout/stderr for `tail -f`. We pick a
/// path the user can predict (and that survives an app restart) so the
/// LoginView's diagnostic panel can show it as a copy-pasteable string.
///
/// Conventions:
///   macOS:   ~/Library/Logs/Nexus/sidecar.log
///   Linux:   ~/.local/state/Nexus/sidecar.log  (XDG state dir)
///   Windows: %APPDATA%\Nexus\logs\sidecar.log
///
/// Distinct from tauri-plugin-log's `nexus.log` (which holds the Tauri
/// front-end's own log::info!/warn! entries with their prefixes). This
/// file is the *raw* server stream, suitable for grep'ing tracebacks.
fn sidecar_log_path() -> PathBuf {
    if cfg!(target_os = "macos") {
        if let Some(home) = dirs_home() {
            return home.join("Library").join("Logs").join("Nexus").join("sidecar.log");
        }
    }
    if cfg!(target_os = "windows") {
        if let Some(appdata) = std::env::var_os("APPDATA") {
            return PathBuf::from(appdata).join("Nexus").join("logs").join("sidecar.log");
        }
    }
    if let Some(home) = dirs_home() {
        return home.join(".local").join("state").join("Nexus").join("sidecar.log");
    }
    PathBuf::from("sidecar.log")
}

/// Unix-seconds (u64) — used by SidecarDiag's ring buffer entries so the
/// frontend can format them locally as relative timestamps ("3s ago").
fn unix_now_secs_u64() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Parse a dotenv file into a flat KEY→VALUE map. Mirrors v1's start.sh
/// behaviour (lines 220-247): split on the FIRST '=', skip blank lines
/// and lines starting with '#', strip ONE pair of surrounding quotes
/// from the value. Returns an empty map and logs a warning if the file
/// is missing — the sidecar still boots, just without LLM keys, and the
/// frontend's Settings · LLM dialog can write the file later.
fn load_user_env(path: &Path) -> HashMap<String, String> {
    let mut out: HashMap<String, String> = HashMap::new();
    let text = match fs::read_to_string(path) {
        Ok(t) => t,
        Err(_) => {
            log::warn!("no user .env at {} — sidecar will run with defaults only", path.display());
            return out;
        }
    };
    let mut count_loaded = 0usize;
    for line in text.lines() {
        let trimmed = line.trim_start();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let eq = match line.find('=') {
            Some(i) => i,
            None => continue,
        };
        let key = line[..eq].trim();
        if key.is_empty() {
            continue;
        }
        let mut val = &line[eq + 1..];
        // Strip a matching pair of surrounding quotes.
        let bytes = val.as_bytes();
        if val.len() >= 2
            && ((bytes[0] == b'"'  && bytes[bytes.len() - 1] == b'"')
             || (bytes[0] == b'\'' && bytes[bytes.len() - 1] == b'\''))
        {
            val = &val[1..val.len() - 1];
        }
        out.insert(key.to_string(), val.to_string());
        count_loaded += 1;
    }
    log::info!("loaded {} env var(s) from {}", count_loaded, path.display());
    out
}

/// Seed or delta-merge the user's .env from the bundled default.env.
///
/// Mirrors v1's setup.sh + start.sh combined behaviour:
///
///   - User .env missing  → full copy from bundled default (first install).
///   - User .env exists   → walk every KEY= line in the bundle; for any
///                          key not already present (commented OR
///                          uncommented) in the user file, append it.
///                          Values the user has overridden locally
///                          (e.g. a GEMINI_API_KEY they swapped in via
///                          Settings · LLM) are preserved.
///
/// Idempotent: safe to call every launch. The .dmg auto-update story
/// rides on this — when a new build ships with a new NEXUS_RELAY_URL or
/// rotated key, the next launch's delta-merge picks it up without
/// asking the medic.
fn seed_or_merge_user_env(app: &AppHandle, user_env_path: &Path) -> Result<usize, String> {
    // Locate the bundled default.env. Tauri resolves it to the .app's
    // Resources/_up_/resources/default.env on macOS; in `pnpm tauri:dev`
    // it points at the on-disk file directly.
    let bundled = match app
        .path()
        .resolve("resources/default.env", tauri::path::BaseDirectory::Resource)
    {
        Ok(p) => p,
        Err(e) => {
            log::warn!("default.env not bundled — skipping seed/merge ({e})");
            return Ok(0);
        }
    };
    let bundled_text = match fs::read_to_string(&bundled) {
        Ok(t) => t,
        Err(e) => {
            log::warn!("could not read bundled default.env: {e}");
            return Ok(0);
        }
    };

    // First install — full seed.
    if !user_env_path.exists() {
        if let Some(parent) = user_env_path.parent() {
            fs::create_dir_all(parent).map_err(|e| format!("mkdir {}: {e}", parent.display()))?;
        }
        let header = format!(
            "# Nexus runtime config — seeded by Tauri on first launch.\n\
             # Edit directly to override (e.g. swap GEMINI_API_KEY) or use\n\
             # Settings · LLM in the desktop. New keys shipped in future\n\
             # .dmg releases are merged in automatically on launch.\n\n"
        );
        let mut f = fs::File::create(user_env_path)
            .map_err(|e| format!("create {}: {e}", user_env_path.display()))?;
        f.write_all(header.as_bytes())
            .and_then(|_| f.write_all(bundled_text.as_bytes()))
            .map_err(|e| format!("write {}: {e}", user_env_path.display()))?;
        // Tighten permissions — file holds API keys.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = fs::set_permissions(user_env_path, fs::Permissions::from_mode(0o600));
        }
        let lines = bundled_text.lines().count();
        log::info!(
            "seeded {} from bundled default.env ({} lines)",
            user_env_path.display(), lines,
        );
        return Ok(lines);
    }

    // Existing install — collect KEYs from bundle, find ones missing
    // from user .env, append them under a dated header.
    let user_text = fs::read_to_string(user_env_path)
        .map_err(|e| format!("read {}: {e}", user_env_path.display()))?;
    let user_has_key = |k: &str| -> bool {
        for line in user_text.lines() {
            let mut t = line.trim_start();
            if t.starts_with('#') {
                t = t[1..].trim_start();   // allow commented-out form
            }
            if let Some(eq) = t.find('=') {
                if t[..eq].trim() == k {
                    return true;
                }
            }
        }
        false
    };

    let mut to_append: Vec<&str> = Vec::new();
    for line in bundled_text.lines() {
        let trimmed = line.trim_start();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let Some(eq) = line.find('=') else { continue };
        let key = line[..eq].trim();
        if key.is_empty() {
            continue;
        }
        if !user_has_key(key) {
            to_append.push(line);
        }
    }

    if to_append.is_empty() {
        log::info!("user .env already has every bundled key — no merge needed");
        return Ok(0);
    }

    let now = unix_now_secs();
    let header = format!(
        "\n# ── Bundle merge {} (Tauri startup) — added {} new key(s) ─\n",
        now, to_append.len()
    );
    let mut f = fs::OpenOptions::new()
        .append(true)
        .open(user_env_path)
        .map_err(|e| format!("append {}: {e}", user_env_path.display()))?;
    f.write_all(header.as_bytes())
        .and_then(|_| {
            for line in &to_append {
                f.write_all(line.as_bytes())?;
                f.write_all(b"\n")?;
            }
            Ok(())
        })
        .map_err(|e| format!("merge-write {}: {e}", user_env_path.display()))?;
    log::info!(
        "merged {} new key(s) from bundled default.env into {}",
        to_append.len(), user_env_path.display(),
    );
    Ok(to_append.len())
}

/// Unix-seconds timestamp as a string. Used as a build-merge marker
/// in .env headers ("# ── Bundle merge 1718312480 …"). No external
/// crate dep — full calendar arithmetic isn't worth pulling chrono.
fn unix_now_secs() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
        .to_string()
}

/// Launch the bundled nexus-server binary. Streams its stdout/stderr
/// into the Tauri log AND into the SidecarDiag ring buffer + on-disk
/// log file so the LoginView's "Cannot reach server" diagnostic panel
/// can show the user exactly why the backend didn't come up.
fn spawn_backend_sidecar(app: &AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    log::info!("spawning nexus-server sidecar");

    let diag: State<SidecarDiag> = app.state();
    let diag_clone = diag.inner().clone();
    diag.push("sys", "preparing sidecar spawn".to_string());

    // v1-parity key handling: read $RUNE_HOME/.env and inject every
    // KEY=VALUE pair into the sidecar's environment, matching what the
    // legacy Avalonia installer's start.sh did (see git tag
    // legacy/avalonia-final). Without this step, the bundled .app
    // launched from Finder/Dock sees an empty os.environ and
    // config.GEMINI_API_KEY = None, which makes every LLM-using
    // endpoint 500 (the medic sees "Backend unreachable" because the
    // chat request fails before turn_started).
    let rh = rune_home();
    let env_path = rh.join(".env");
    diag.push("sys", format!("rune_home = {}", rh.display()));
    diag.push("sys", format!("env file  = {}", env_path.display()));

    // Seed (first install) or delta-merge (every launch) the user .env
    // from the bundled default. This is what makes "reinstall the .dmg
    // and the new keys / new server code flow in" work — the keys side
    // happens here; the code side happens because the .dmg ships a
    // fresh PyInstaller binary at src-tauri/binaries/nexus-server-*.
    match seed_or_merge_user_env(app, &env_path) {
        Ok(n) => diag.push("sys", format!("env seed/merge ok ({n} new key(s))")),
        Err(e) => {
            log::warn!("env seed/merge failed: {e}");
            diag.push("sys", format!("env seed/merge failed: {e}"));
        }
    }

    let user_env = load_user_env(&env_path);
    diag.push("sys", format!("env loaded: {} key(s)", user_env.len()));
    log::info!("rune_home: {}", rh.display());

    // Port-preflight: if a previous sidecar died without cleanup (app
    // crash / force-quit), an orphan nexus-server may still hold 8001
    // and the fresh spawn dies with EADDRINUSE after a full (and
    // confusing) successful-looking boot. Find holders of the port and
    // kill them — but ONLY processes whose command name looks like our
    // sidecar, so we never kill an unrelated dev server.
    #[cfg(unix)]
    {
        use std::process::Command;
        let port = std::env::var("NEXUS_PORT").unwrap_or_else(|_| "8001".into());
        if let Ok(out) = Command::new("lsof")
            .args(["-ti", &format!("tcp:{port}")])
            .output()
        {
            for pid in String::from_utf8_lossy(&out.stdout).split_whitespace() {
                let comm = Command::new("ps")
                    .args(["-p", pid, "-o", "comm="])
                    .output()
                    .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
                    .unwrap_or_default();
                if comm.contains("nexus-server") || comm.contains("nexus_server") {
                    diag.push(
                        "sys",
                        format!("port {port} held by orphan sidecar pid={pid} ({comm}) — killing"),
                    );
                    let _ = Command::new("kill").args(["-9", pid]).output();
                } else if !comm.is_empty() {
                    diag.push(
                        "sys",
                        format!(
                            "WARNING: port {port} held by unrelated process pid={pid} ({comm}) — \
                             not killing; sidecar bind will fail"
                        ),
                    );
                }
            }
        }
    }

    let mut sidecar = match app.shell().sidecar("nexus-server") {
        Ok(c) => c,
        Err(e) => {
            // This is the single most useful failure mode to surface
            // explicitly: the bundled binary path didn't resolve.
            // Usually means the PyInstaller build never ran, or the
            // triple in `binaries/nexus-server-<triple>` doesn't
            // match this Mac (e.g. Apple Silicon binary on Intel).
            let msg = format!("failed to resolve sidecar binary: {e}");
            log::error!("{msg}");
            diag.push("sys", msg.clone());
            return Err(msg.into());
        }
    };
    sidecar = sidecar
        // F-bind-127 — bind to deterministic IPv4 loopback. F19
        // experimented with the DNS name ``localhost``, but on macOS
        // / dual-stack systems ``localhost``
        // can resolve to BOTH 127.0.0.1 (IPv4) and ::1 (IPv6).
        // uvicorn binds to whichever the resolver returns first; if
        // the browser then tries the other address we get an opaque
        // "Backend unreachable" splash.
        //
        // Solution: server binds 127.0.0.1 (deterministic); frontend
        // baseUrl is still ``http://localhost:8001`` (DNS name). The
        // browser's resolver maps ``localhost`` to 127.0.0.1 (default
        // priority on macOS / Win / most Linux) and hits the bound
        // socket. The page's effective domain is still ``localhost``
        // (the URL), regardless of the socket address.
        .env("NEXUS_HOST", "127.0.0.1")
        .env("NEXUS_PORT", "8001")
        // CORS: the bundled webview runs from tauri://localhost (or
        // asset://localhost on some platforms). Backend defaults only
        // include localhost:3000 and :5173 (dev origins). Setting
        // wildcard is safe here because the backend is bound to
        // 127.0.0.1 (loopback only — not reachable off-host) AND
        // every protected route still requires a valid JWT.
        .env("CORS_ALLOW_ORIGINS", "*")
        // RUNE_HOME so the sidecar's settings router knows where to
        // read/write the .env when the medic updates a key.
        .env("RUNE_HOME", rh.to_string_lossy().to_string())
        // Python: force unbuffered output so every print / traceback
        // line reaches our drain immediately. Without this, PyInstaller
        // can buffer up to 8 KiB and a crash that prints just one
        // exception line leaves us with an empty diag panel.
        .env("PYTHONUNBUFFERED", "1")
        // F-alembic-ascii: a fresh-from-the-DMG sidecar process inherits
        // LANG=C from launchctl on macOS when the user has never opened
        // Terminal. Python's configparser then uses the ASCII codec for
        // text-mode `open()` calls, and any file with non-ASCII bytes
        // (em-dashes, smart quotes, CJK comments) blows up with
        // UnicodeDecodeError before our app even starts. PYTHONUTF8=1
        // is Python 3.7+'s UTF-8 mode -- it forces ALL stdlib text I/O
        // (including configparser, json, csv) to UTF-8 regardless of
        // locale. LANG / LC_ALL are belt-and-suspenders for any C
        // extensions that read locale directly.
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("LANG", "en_US.UTF-8")
        .env("LC_ALL", "en_US.UTF-8");

    for (k, v) in user_env {
        // Don't overwrite the loopback host/port we just set above.
        if k == "NEXUS_HOST" || k == "NEXUS_PORT" || k == "CORS_ALLOW_ORIGINS" {
            continue;
        }
        sidecar = sidecar.env(k, v);
    }

    let (mut rx, child) = match sidecar.spawn() {
        Ok(p) => p,
        Err(e) => {
            let msg = format!("spawn() failed: {e}");
            log::error!("{msg}");
            diag.push("sys", msg.clone());
            return Err(msg.into());
        }
    };

    let pid = child.pid();
    log::info!("nexus-server sidecar pid={}", pid);
    diag.push("sys", format!("sidecar spawned, pid={pid}"));
    diag.mark_spawned(pid);

    // Stash the child so we can kill it on exit.
    let state: State<SidecarState> = app.state();
    {
        let mut guard = state.0.lock().unwrap();
        *guard = Some(child);
    }

    // Drain stdout/stderr → both app log AND the SidecarDiag ring buffer
    // / on-disk file. The two destinations serve different audiences:
    //   - log:: → tauri-plugin-log → ~/Library/Logs/<bundle>/nexus.log
    //     (mixed front+back, useful for engineers tailing the tauri
    //     side too)
    //   - diag.push() → ~/Library/Logs/Nexus/sidecar.log (raw server
    //     stream only, what the LoginView panel shows)
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    let s = String::from_utf8_lossy(&line).to_string();
                    log::info!("[server] {s}");
                    diag_clone.push("stdout", s);
                }
                CommandEvent::Stderr(line) => {
                    let s = String::from_utf8_lossy(&line).to_string();
                    log::warn!("[server] {s}");
                    diag_clone.push("stderr", s);
                }
                CommandEvent::Error(msg) => {
                    log::error!("[server] sidecar error: {msg}");
                    diag_clone.push("sys", format!("error: {msg}"));
                }
                CommandEvent::Terminated(payload) => {
                    log::error!("[server] sidecar terminated: code={:?}", payload.code);
                    diag_clone.push(
                        "sys",
                        format!("sidecar terminated, exit code={:?}", payload.code),
                    );
                    diag_clone.mark_exited(payload.code);
                }
                _ => {}
            }
        }
    });

    Ok(())
}

/// IPC probe — frontend can call this to verify the bridge is alive.
/// The frontend additionally polls /api/v1/memory/_status via HTTP
/// for backend liveness.
#[tauri::command]
fn server_health() -> Result<String, String> {
    Ok("ok".to_string())
}

/// Structured diagnostics for the LoginView "Cannot reach server" panel.
///
/// Returns:
///   {
///     "pid":            u32 | null,         currently-tracked pid
///     "alive":          bool,                spawned and not yet Terminated
///     "last_exit_code": i32 | null,          set when the sidecar died
///     "started_at":     u64,                 unix seconds at last spawn
///     "log_path":       string,              path the user can `tail -f`
///     "lines":          DiagLine[],          ring buffer, newest last
///   }
///
/// The frontend uses this both during login (when health probe fails)
/// and from Settings · Diagnostics (a future tab) for ad-hoc inspection.
#[tauri::command]
fn get_sidecar_diagnostics(diag: State<SidecarDiag>) -> serde_json::Value {
    diag.snapshot()
}

/// Direct read of the user's .env state, bypassing the FastAPI server.
/// This is the fallback Settings · LLM uses when the backend's
/// GET /api/v1/settings/llm 404s (stale binary predates U3.3).
///
/// Returns key-presence booleans + the resolved env path. We never
/// return key VALUES — same contract as the backend.
#[tauri::command]
fn llm_env_status() -> serde_json::Value {
    let path = rune_home().join(".env");
    let env = load_user_env(&path);

    let has_key = |k: &str| {
        env.get(k)
            .map(|v| !v.trim().is_empty())
            .unwrap_or(false)
    };

    serde_json::json!({
        "provider":         env.get("DEFAULT_LLM_PROVIDER").cloned().unwrap_or_else(|| "gemini".into()),
        "model":            env.get("DEFAULT_LLM_MODEL").cloned().unwrap_or_else(|| "gemini-2.5-flash".into()),
        "env_file_path":    path.to_string_lossy().to_string(),
        "env_file_exists":  path.exists(),
        "has_gemini_key":   has_key("GEMINI_API_KEY"),
        "has_openai_key":   has_key("OPENAI_API_KEY"),
        "has_anthropic_key":has_key("ANTHROPIC_API_KEY"),
        "has_kimi_key":     has_key("KIMI_API_KEY") || has_key("MOONSHOT_API_KEY"),
        // serde_json::json! takes JSON-literal tokens, not Rust generics —
        // ``null`` is what you write for an explicit null.
        "advisory":         null,
    })
}

/// Direct write to ~/.../RuneProtocol/.env, used when the backend's
/// PUT /api/v1/settings/llm is unavailable. Mirrors the backend's
/// idempotent-merge semantics: for each ``updates`` key, replace any
/// existing assignment in place, else append under a dated header.
///
/// Atomic via tempfile + rename so a crash mid-write can't truncate
/// the file the next launch needs.
#[tauri::command]
fn llm_env_write(updates: HashMap<String, String>) -> Result<serde_json::Value, String> {
    let path = rune_home().join(".env");
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("mkdir {}: {e}", parent.display()))?;
    }
    let existing = fs::read_to_string(&path).unwrap_or_default();

    let mut remaining: HashMap<String, String> = updates.clone();
    let mut new_lines: Vec<String> = Vec::new();
    for line in existing.lines() {
        let mut replaced = false;
        // For each pending key, see if this line starts with KEY=.
        for k in remaining.keys().cloned().collect::<Vec<_>>() {
            let stripped = line.trim_start();
            if let Some(eq) = stripped.find('=') {
                if stripped[..eq].trim() == k {
                    new_lines.push(format!("{}={}", k, remaining.remove(&k).unwrap()));
                    replaced = true;
                    break;
                }
            }
        }
        if !replaced {
            new_lines.push(line.to_string());
        }
    }
    if !remaining.is_empty() {
        if !new_lines.is_empty() && !new_lines.last().map(|l| l.trim().is_empty()).unwrap_or(true) {
            new_lines.push(String::new());
        }
        new_lines.push(format!(
            "# ── Settings · LLM (written via Tauri IPC at unix {}) ──",
            unix_now_secs(),
        ));
        for (k, v) in &remaining {
            new_lines.push(format!("{}={}", k, v));
        }
    }

    // Atomic write: tempfile + rename. ``Path::with_extension`` is
    // unsafe for our filename — for ``/path/.env``, Rust treats the
    // whole name as the stem, so with_extension("env.tmp") produces
    // ``/path/.env.env.tmp``. Build the sibling path explicitly.
    let tmp = path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join(".env.tmp");
    {
        let mut f = fs::File::create(&tmp)
            .map_err(|e| format!("create {}: {e}", tmp.display()))?;
        f.write_all(new_lines.join("\n").as_bytes())
            .and_then(|_| if new_lines.is_empty() { Ok(()) } else { f.write_all(b"\n") })
            .map_err(|e| format!("write {}: {e}", tmp.display()))?;
    }
    fs::rename(&tmp, &path).map_err(|e| format!("rename {}: {e}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&path, fs::Permissions::from_mode(0o600));
    }

    let written: Vec<String> = updates.keys().cloned().collect();
    Ok(serde_json::json!({
        "ok": true,
        "env_file_path": path.to_string_lossy().to_string(),
        "written_keys": written,
        "status": llm_env_status(),
    }))
}

/// Kill the running sidecar and respawn it. Used by Settings · LLM's
/// "Apply now" button to force the FastAPI process to re-read the
/// freshly-written .env (config.GEMINI_API_KEY is captured at import,
/// so the existing process keeps using the old value until restart).
#[tauri::command]
fn restart_sidecar(app: AppHandle) -> Result<String, String> {
    log::info!("restart_sidecar: killing current child");
    {
        let diag: State<SidecarDiag> = app.state();
        diag.push("sys", "restart_sidecar invoked — killing current child".to_string());
    }
    {
        let state: State<SidecarState> = app.state();
        // Same E0597 dance as the exit handler at the top of this
        // file: ``state.0.lock()`` returns a Result whose Err variant
        // holds a MutexGuard borrowed from ``state``. As an unnamed
        // temporary in the ``if let`` scrutinee, the Result outlives
        // ``state`` and the borrow checker rejects the drop order.
        // Binding to a named local pins both lifetimes to the block.
        let lock_result = state.0.lock();
        if let Ok(mut guard) = lock_result {
            if let Some(child) = guard.take() {
                let _ = child.kill();
            }
        }
    }
    // Brief pause so the OS releases the port before respawn.
    std::thread::sleep(std::time::Duration::from_millis(400));
    spawn_backend_sidecar(&app).map_err(|e| format!("respawn failed: {e}"))?;
    Ok("restarted".to_string())
}


/// Read the current server mode config and return it to the frontend.
///
/// Returns:
///   { "mode": "local" | "remote",
///     "remote_url": string | null,
///     "base_url": string }   ← ready-to-use URL for fetch calls
///
/// ``base_url`` is always filled in:
///   - local  → "http://localhost:8001"
///   - remote → remote_url (or "" if not yet configured)
#[tauri::command]
fn get_server_mode() -> serde_json::Value {
    let cfg = read_server_config();
    let base_url = if cfg.mode == "remote" {
        cfg.remote_url.clone().unwrap_or_default()
    } else {
        "http://localhost:8001".to_string()
    };
    serde_json::json!({
        "mode":       cfg.mode,
        "remote_url": cfg.remote_url,
        "base_url":   base_url,
    })
}

/// Persist a new server-mode config. The change takes effect on the
/// NEXT launch — a restart is needed because:
///   - local→remote: we need to skip sidecar spawn on restart
///   - remote→local: we need to spawn the sidecar on restart
///
/// Returns ``{ ok: true, mode, restart_required: true }`` on success.
#[tauri::command]
fn set_server_mode(mode: String, url: Option<String>) -> Result<serde_json::Value, String> {
    if mode != "local" && mode != "remote" {
        return Err(format!(
            "invalid mode {mode:?}; must be \"local\" or \"remote\""
        ));
    }
    if mode == "remote" && url.as_ref().map_or(true, |u| u.trim().is_empty()) {
        return Err("remote mode requires a non-empty url".to_string());
    }
    let remote_url = if mode == "remote" { url } else { None };
    write_server_config(ServerConfigFile { mode: mode.clone(), remote_url })?;
    log::info!("set_server_mode: saved mode={mode}; restart required");
    Ok(serde_json::json!({
        "ok":              true,
        "mode":            mode,
        "restart_required": true,
    }))
}

// F24 — the previous OS-Keychain implementation was removed in favour
// of a backend-managed identity file at ``$RUNE_HOME/identity.json``.
// Reasoning: storing a UUID user_id in macOS Keychain triggered a
// permission dialog on first launch (one system prompt the medic
// doesn't expect from a local clinical tool). The user_id isn't
// secret in the password sense — it's an opaque identifier scoped
// to the backend's own user-data directory, which is already private
// to the macOS user account at the filesystem level. Backend handles
// the read/write in packages/server/nexus_server/auth/routes.py
// (POST /api/v1/auth/local-bootstrap). Frontend just makes that HTTP
// call — no Tauri IPC, no keychain dependency, no permission prompt.

// ──────────────────────────────────────────────────────────────────────
// Tests — `cargo test -p nexus-desktop-v2`
// ──────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_log_path(suffix: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("nexus-sidecar-test-{suffix}-{}.log", unix_now_secs_u64()));
        p
    }

    /// `SidecarDiag::push` must:
    ///   1. Append to the in-memory ring buffer.
    ///   2. Tag the entry with the stream marker we passed in.
    ///   3. Write the same line to the on-disk log file.
    /// Without (3), the LoginView diagnostic panel would show a useful
    /// tail but `tail -f` would show nothing — defeating the point of
    /// surfacing the path to the user.
    #[test]
    fn push_writes_to_buffer_and_disk() {
        let diag = SidecarDiag::default();
        let path = tmp_log_path("push");
        {
            let mut g = diag.0.lock().unwrap();
            g.log_path = path.clone();
        }

        diag.push("stderr", "boom: ModuleNotFoundError: alembic".into());

        let g = diag.0.lock().unwrap();
        assert_eq!(g.buffer.len(), 1);
        let line = &g.buffer[0];
        assert_eq!(line.stream, "stderr");
        assert!(line.text.contains("ModuleNotFoundError"));
        drop(g);

        let on_disk = fs::read_to_string(&path).expect("log file written");
        assert!(on_disk.contains("[stderr]"));
        assert!(on_disk.contains("ModuleNotFoundError"));
        let _ = fs::remove_file(&path);
    }

    /// Once the ring buffer hits RING_CAP it must drop the OLDEST line,
    /// not refuse new ones. A common failure mode for sidecar tracebacks
    /// is "thousands of repeating retry lines" — without a bounded ring
    /// the diag IPC would leak memory until the user quits the app.
    #[test]
    fn ring_buffer_rotates_at_cap() {
        let diag = SidecarDiag::default();
        // Don't write to disk in this test — set log_path to a path that
        // can't be opened (parent doesn't exist + no create perm) so
        // push() short-circuits the file write but still mutates the
        // ring. The `if let Ok(...)` in push() makes file IO best-effort.
        {
            let mut g = diag.0.lock().unwrap();
            g.log_path = PathBuf::from("/this/dir/does/not/exist/x.log");
        }
        for i in 0..(SidecarDiag::RING_CAP + 25) {
            diag.push("stdout", format!("line-{i}"));
        }
        let g = diag.0.lock().unwrap();
        assert_eq!(g.buffer.len(), SidecarDiag::RING_CAP,
                   "ring buffer must cap at RING_CAP, not grow unbounded");
        let oldest = &g.buffer[0];
        // We pushed RING_CAP+25 lines, so the oldest remaining is line-25.
        assert_eq!(oldest.text, "line-25");
        let newest = g.buffer.back().unwrap();
        assert_eq!(newest.text, format!("line-{}", SidecarDiag::RING_CAP + 24));
    }

    /// `mark_spawned` then `mark_exited` must update the structured
    /// fields the frontend renders ("EXITED · code 1 · 3s ago"). The
    /// diag panel reads these for the one-line status summary; if they
    /// don't update, the user sees a stale "running" claim next to a
    /// dead sidecar.
    #[test]
    fn lifecycle_marks_update_status_fields() {
        let diag = SidecarDiag::default();
        {
            let mut g = diag.0.lock().unwrap();
            g.log_path = tmp_log_path("lifecycle");
        }

        diag.mark_spawned(42);
        {
            let g = diag.0.lock().unwrap();
            assert_eq!(g.pid, Some(42));
            assert!(g.alive);
            assert_eq!(g.last_exit_code, None);
            assert!(g.started_at > 0);
        }

        diag.mark_exited(Some(1));
        {
            let g = diag.0.lock().unwrap();
            assert!(!g.alive);
            assert_eq!(g.last_exit_code, Some(1));
            // pid stays — the LoginView still wants to show "pid 42 exited"
            assert_eq!(g.pid, Some(42));
        }
    }

    /// `snapshot()` must return all the JSON keys the TypeScript
    /// `SidecarDiagnostics` interface expects. If anyone renames a
    /// field, this test fires before the frontend silently breaks.
    #[test]
    fn snapshot_has_all_documented_keys() {
        let diag = SidecarDiag::default();
        diag.push("sys", "hello".into());

        let v = diag.snapshot();
        let obj = v.as_object().expect("snapshot returns an object");
        for key in ["pid", "alive", "last_exit_code", "started_at",
                    "log_path", "lines"] {
            assert!(obj.contains_key(key),
                    "snapshot is missing key '{key}' — frontend will break");
        }
        let lines = obj["lines"].as_array().expect("lines is an array");
        assert_eq!(lines.len(), 1);
        let line = lines[0].as_object().expect("each line is an object");
        for key in ["ts", "stream", "text"] {
            assert!(line.contains_key(key),
                    "DiagLine is missing key '{key}'");
        }
    }
}
