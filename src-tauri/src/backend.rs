use std::{
    collections::HashSet,
    env,
    error::Error,
    ffi::{OsStr, OsString},
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    net::TcpListener,
    os::fd::AsRawFd,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::Mutex,
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use crate::persistent_daemon;
use reqwest::{blocking::Client, Url};
use tauri::{
    ipc::CapabilityBuilder, webview::NewWindowResponse, App, AppHandle, Manager, RunEvent,
    WebviewUrl, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
use tauri_plugin_opener::OpenerExt;
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent, TerminatedPayload},
    ShellExt,
};

const FINDER_SAFE_PATH: &str = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin";
const HEALTH_WAIT_TIMEOUT: Duration = Duration::from_secs(15);
const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(200);
const SHUTDOWN_WAIT_TIMEOUT: Duration = Duration::from_secs(3);
const MAIN_WINDOW_LABEL: &str = "main";
const WINDOW_TITLE: &str = "Wiki";
const APP_LOCK_NAME: &str = "app.lock";
const EXPECTED_BACKEND_FINGERPRINT: &str = env!("WIKI_EXPECTED_BACKEND_FINGERPRINT");
// Guard: WKWebView can defer this eval past the post-health navigate() when
// the backend boots fast (onedir sidecar ~0.4s) — unguarded, the deferred
// write CLOBBERS the already-loaded app with the static loading card.
// Only paint the card while still on the initial about:blank document.
const LOADING_PAGE: &str = r#"if (location.protocol === "about:") {
document.open();
document.write(`<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Wiki</title>
    <style>
      :root {
        color-scheme: light dark;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      body {
        margin: 0;
        background: #f6f5f1;
        color: #1f2328;
      }
      .shell {
        min-height: 100vh;
        display: grid;
        place-items: center;
        padding: 24px;
      }
      .card {
        width: min(320px, 100%);
        border: 1px solid rgba(15, 23, 42, 0.12);
        background: rgba(255, 255, 255, 0.92);
        padding: 18px 20px;
      }
      .label {
        display: inline-block;
        margin-bottom: 10px;
        padding: 3px 8px;
        border: 1px solid rgba(15, 23, 42, 0.12);
        color: #5b6470;
        font-size: 11px;
        letter-spacing: 0.04em;
        text-transform: uppercase;
      }
      h1 {
        margin: 0 0 6px;
        font-size: 16px;
        font-weight: 600;
      }
      p {
        margin: 0;
        color: #5b6470;
        font-size: 13px;
        line-height: 1.5;
      }
      @media (prefers-color-scheme: dark) {
        body {
          background: #12161c;
          color: #e6edf3;
        }
        .card {
          border-color: rgba(230, 237, 243, 0.12);
          background: rgba(25, 30, 38, 0.94);
        }
        .label,
        p {
          color: #9da7b3;
          border-color: rgba(230, 237, 243, 0.12);
        }
      }
    </style>
  </head>
  <body>
    <main class="shell">
      <section class="card">
        <span class="label">Starting</span>
        <h1>Launching backend</h1>
        <p>Waiting for the local API to become healthy.</p>
      </section>
    </main>
  </body>
</html>`);
document.close();
}"#;
// Stable default keeps the webview origin constant across launches so
// localStorage (origin-scoped) survives. Falls back to a random port only if
// the bind fails (e.g. another wiki instance is running).
const DEFAULT_LOOPBACK_PORT: u16 = 8213;

#[derive(Default)]
pub struct NativeAppState {
    inner: Mutex<LifecycleState>,
    // The GUI owns this descriptor for its whole lifetime. Keeping the file
    // handle in managed state makes sidecar restarts unable to release the
    // app-lifetime exclusion lock.
    _app_lock: Option<File>,
}

#[derive(Default)]
struct LifecycleState {
    app_origin: Option<String>,
    sidecar: Option<SidecarState>,
    // Wiki.app origin secret received through the sidecar stdin pipe or the
    // daemon's code-identity-authenticated runtime channel. Held in Rust
    // process memory only.
    // WIKI-148 round 6, Path B.
    wiki_app_secret: Option<String>,
    // Loopback origins for which a remote-scoped ACL capability granting
    // `allow-get-wiki-app-secret` has already been registered via
    // `AppHandle::add_capability`. Sidecar restarts pick a fresh port and
    // therefore need a fresh capability; tracking prevents duplicate
    // registration for the same origin. WIKI-148 round 7.
    ipc_authorized_origins: HashSet<String>,
    daemon_managed: bool,
}

struct SidecarState {
    child: Option<CommandChild>,
    pid: u32,
    log_path: PathBuf,
    healthy_started: bool,
    restart_count: u8,
    shutting_down: bool,
    last_termination: Option<String>,
}

enum SidecarAction {
    Restart,
    ShowError(String, PathBuf),
}

pub fn setup(app: &mut App, app_lock: File) -> Result<(), Box<dyn Error>> {
    app.manage(NativeAppState {
        _app_lock: Some(app_lock),
        ..NativeAppState::default()
    });

    let app_handle = app.handle().clone();
    let window = WebviewWindowBuilder::new(
        &app_handle,
        MAIN_WINDOW_LABEL,
        WebviewUrl::External("about:blank".parse()?),
    )
    .title(WINDOW_TITLE)
    .inner_size(1400.0, 950.0)
    .resizable(true)
    .on_navigation({
        let app_handle = app_handle.clone();
        move |url| handle_navigation_request(&app_handle, url)
    })
    .on_new_window({
        let app_handle = app_handle.clone();
        move |url, _features| handle_new_window_request(&app_handle, &url)
    })
    // Tauri's native drag-drop handler intercepts drag events and breaks
    // HTML5 DnD (kanban, pane splits) inside the webview — disable it.
    .disable_drag_drop_handler()
    .build()?;

    let _ = window.eval(LOADING_PAGE);

    let launch_handle = app_handle.clone();
    thread::spawn(move || launch_backend_and_navigate(&launch_handle));

    Ok(())
}

fn runtime_dir() -> PathBuf {
    env::var_os("WIKI_AGENT_RUNTIME_DIR")
        .map(PathBuf::from)
        .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".wiki/agent-runtime")))
        .unwrap_or_else(|| PathBuf::from(".wiki/agent-runtime"))
}

pub fn acquire_app_lock() -> io::Result<File> {
    let runtime_dir = runtime_dir();
    fs::create_dir_all(&runtime_dir)?;
    fs::set_permissions(&runtime_dir, fs::Permissions::from_mode(0o700))?;
    let path = runtime_dir.join(APP_LOCK_NAME);
    let file = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .open(&path)?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;

    let result = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if result == 0 {
        return Ok(file);
    }

    let error = io::Error::last_os_error();
    let code = error.raw_os_error();
    if code == Some(libc::EWOULDBLOCK) || code == Some(libc::EAGAIN) {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            format!(
                "Wiki.app is already running (app lock held at {})",
                path.display()
            ),
        ));
    }
    Err(io::Error::new(
        error.kind(),
        format!(
            "cannot acquire Wiki.app lock at {}: {error}",
            path.display()
        ),
    ))
}

pub fn show_already_running_dialog() {
    let script = r#"display dialog "Wiki is already running.\n\nThe existing Wiki window remains active." with title "Wiki" buttons {"OK"} default button "OK" with icon caution"#;
    if let Err(error) = std::process::Command::new("/usr/bin/osascript")
        .args(["-e", script])
        .status()
    {
        eprintln!("Wiki is already running; unable to show native dialog: {error}");
    }
}

pub fn handle_window_event(window: &tauri::Window, event: &WindowEvent) {
    if window.label() != MAIN_WINDOW_LABEL {
        return;
    }
    if let WindowEvent::CloseRequested { api, .. } = event {
        api.prevent_close();
        shutdown_sidecar(&window.app_handle());
        window.app_handle().exit(0);
    }
}

pub fn handle_run_event(app: &AppHandle, event: RunEvent) {
    match event {
        RunEvent::ExitRequested { .. } | RunEvent::Exit => shutdown_sidecar(app),
        _ => {}
    }
}

fn start_sidecar(app: &AppHandle, restart_count: u8) -> Result<String, Box<dyn Error>> {
    let port = pick_loopback_port()?;
    let app_secret = new_app_secret()?;
    let launch_url = format!("http://127.0.0.1:{port}/");
    let repo_dir = resolve_repo_dir();
    let vault_dir = resolve_vault_dir(&repo_dir);
    let log_path = current_log_path(app)?;
    let parent_pid = std::process::id().to_string();
    let port_string = port.to_string();
    let repo_dir_string = repo_dir.display().to_string();
    let vault_dir_string = vault_dir.display().to_string();

    append_log(
        &log_path,
        &format!(
            "starting wiki-backend on {launch_url} repo={}",
            repo_dir.display()
        ),
    )?;

    let inherited_env = sidecar_environment_without_secret();
    let (rx, mut child) = app
        .shell()
        .sidecar("wiki-backend")?
        .current_dir(&repo_dir)
        .env_clear()
        .envs(inherited_env)
        .env("PATH", FINDER_SAFE_PATH)
        .env("WIKI_REPO_DIR", &repo_dir)
        .env("WIKI_VAULT_DIR", &vault_dir)
        .args([
            "--host",
            "127.0.0.1",
            "--port",
            &port_string,
            "--repo-dir",
            &repo_dir_string,
            "--vault-dir",
            &vault_dir_string,
            "--parent-pid",
            &parent_pid,
        ])
        .spawn()?;

    if let Err(error) = child.write(format!("{app_secret}\n").as_bytes()) {
        let _ = child.kill();
        return Err(io::Error::other(format!("cannot send sidecar auth pipe: {error}")).into());
    }

    let pid = child.pid();
    {
        let app_state = app.state::<NativeAppState>();
        let mut state = app_state.inner.lock().unwrap();
        state.wiki_app_secret = Some(app_secret.clone());
        state.daemon_managed = false;
        state.sidecar = Some(SidecarState {
            child: Some(child),
            pid,
            log_path: log_path.clone(),
            healthy_started: false,
            restart_count,
            shutting_down: false,
            last_termination: None,
        });
    }

    spawn_sidecar_logger(app.clone(), pid, log_path.clone(), rx);

    if let Err(err) = wait_for_health(app, &launch_url, Some(pid)) {
        shutdown_sidecar(app);
        let message = format!("{err}\n\nLog: {}", log_path.display());
        show_error_dialog(app, "Wiki backend failed to start", &message);
        return Err(io::Error::other(message).into());
    }

    {
        let app_state = app.state::<NativeAppState>();
        let mut state = app_state.inner.lock().unwrap();
        if let Some(sidecar) = state.sidecar.as_mut() {
            if sidecar.pid == pid {
                sidecar.healthy_started = true;
                sidecar.last_termination = None;
            }
        }
    }
    append_log(&log_path, &format!("backend healthy on {launch_url}"))?;
    set_app_origin(app, &launch_url);

    Ok(launch_url)
}

fn new_app_secret() -> io::Result<String> {
    let mut bytes = [0_u8; 32];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn sidecar_environment_without_secret() -> Vec<(OsString, OsString)> {
    env::vars_os()
        .filter(|(key, _)| key != OsStr::new("WIKI_APP_SECRET"))
        .collect()
}

fn launch_backend_and_navigate(app: &AppHandle) {
    let launch_result = if native_dev_mode() {
        start_sidecar(app, 0)
    } else if let Ok(url) = env::var("WIKI_NATIVE_BACKEND_URL") {
        let launch_url = normalize_launch_url(&url);
        wait_for_health(app, &launch_url, None).map(|_| launch_url)
    } else {
        match persistent_daemon::probe(&runtime_dir(), EXPECTED_BACKEND_FINGERPRINT) {
            Ok(Some((launch_url, secret))) => {
                set_app_secret(app, secret);
                set_daemon_managed(app, true);
                Ok(launch_url)
            }
            Ok(None) => start_sidecar(app, 0),
            Err(error) => Err(io::Error::other(error).into()),
        }
    };

    match launch_result {
        Ok(launch_url) => {
            set_app_origin(app, &launch_url);
            if let Some(window) = app.get_webview_window(MAIN_WINDOW_LABEL) {
                if let Ok(url) = launch_url.parse() {
                    let _ = window.navigate(url);
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }
        }
        Err(err) => {
            show_error_dialog(app, "Wiki backend failed to start", &format!("{err}"));
        }
    }
}

fn native_dev_mode() -> bool {
    native_dev_flag(env::var("WIKI_NATIVE_DEV").ok().as_deref())
}

fn native_dev_flag(value: Option<&str>) -> bool {
    matches!(value, Some("1" | "true" | "yes" | "on"))
}

fn set_app_secret(app: &AppHandle, secret: String) {
    set_app_secret_state(&app.state::<NativeAppState>(), secret);
}

fn set_app_secret_state(state: &NativeAppState, secret: String) {
    let mut guard = state.inner.lock().unwrap();
    guard.wiki_app_secret = Some(secret);
}

fn refresh_daemon_secret(state: &NativeAppState) {
    let daemon_managed = state.inner.lock().unwrap().daemon_managed;
    if let Some(secret) = persistent_daemon::refresh_secret(
        &runtime_dir(),
        daemon_managed,
        EXPECTED_BACKEND_FINGERPRINT,
    ) {
        set_app_secret_state(state, secret);
    }
}

fn set_daemon_managed(app: &AppHandle, daemon_managed: bool) {
    let app_state = app.state::<NativeAppState>();
    let mut state = app_state.inner.lock().unwrap();
    state.daemon_managed = daemon_managed;
}

fn spawn_sidecar_logger(
    app: AppHandle,
    pid: u32,
    log_path: PathBuf,
    mut rx: tauri::async_runtime::Receiver<CommandEvent>,
) {
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    let decoded = decode_line(&line);
                    let _ = append_log(&log_path, &format!("stdout {decoded}"));
                }
                CommandEvent::Stderr(line) => {
                    let _ = append_log(&log_path, &format!("stderr {}", decode_line(&line)));
                }
                CommandEvent::Error(err) => {
                    let _ = append_log(&log_path, &format!("error {err}"));
                }
                CommandEvent::Terminated(payload) => {
                    let summary = termination_summary(&payload);
                    let _ = append_log(&log_path, &format!("terminated {summary}"));
                    let app = app.clone();
                    thread::spawn(move || handle_sidecar_termination(&app, pid, summary));
                    break;
                }
                _ => {}
            }
        }
    });
}

fn handle_sidecar_termination(app: &AppHandle, pid: u32, summary: String) {
    let action = {
        let app_state = app.state::<NativeAppState>();
        let mut state = app_state.inner.lock().unwrap();
        let Some(sidecar) = state.sidecar.as_mut() else {
            return;
        };
        if sidecar.pid != pid {
            return;
        }

        sidecar.child = None;
        sidecar.last_termination = Some(summary.clone());

        if sidecar.shutting_down {
            return;
        }
        if !sidecar.healthy_started {
            return;
        }

        if sidecar.restart_count == 0 {
            sidecar.restart_count = 1;
            Some(SidecarAction::Restart)
        } else {
            Some(SidecarAction::ShowError(summary, sidecar.log_path.clone()))
        }
    };

    match action {
        Some(SidecarAction::Restart) => {
            if let Some(log_path) = app
                .state::<NativeAppState>()
                .inner
                .lock()
                .unwrap()
                .sidecar
                .as_ref()
                .map(|sidecar| sidecar.log_path.clone())
            {
                let _ = append_log(
                    &log_path,
                    "backend exited unexpectedly; attempting one restart",
                );
            }
            match start_sidecar(app, 1) {
                Ok(launch_url) => {
                    if let Some(window) = app.get_webview_window(MAIN_WINDOW_LABEL) {
                        if let Ok(url) = launch_url.parse() {
                            let _ = window.navigate(url);
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                }
                Err(err) => {
                    show_error_dialog(app, "Wiki backend restart failed", &format!("{err}"));
                }
            }
        }
        Some(SidecarAction::ShowError(summary, log_path)) => {
            show_error_dialog(
                app,
                "Wiki backend stopped",
                &format!("{summary}\n\nLog: {}", log_path.display()),
            );
        }
        None => {}
    }
}

fn shutdown_sidecar(app: &AppHandle) {
    let (pid, log_path) = {
        let app_state = app.state::<NativeAppState>();
        let mut state = app_state.inner.lock().unwrap();
        let Some(sidecar) = state.sidecar.as_mut() else {
            return;
        };
        if sidecar.shutting_down {
            return;
        }
        sidecar.shutting_down = true;
        (sidecar.pid, sidecar.log_path.clone())
    };

    let _ = append_log(&log_path, &format!("requesting backend shutdown pid={pid}"));
    request_graceful_shutdown(pid);

    let deadline = Instant::now() + SHUTDOWN_WAIT_TIMEOUT;
    while Instant::now() < deadline {
        let done = {
            let app_state = app.state::<NativeAppState>();
            let state = app_state.inner.lock().unwrap();
            state
                .sidecar
                .as_ref()
                .is_none_or(|sidecar| sidecar.pid != pid || sidecar.child.is_none())
        };
        if done {
            return;
        }
        thread::sleep(Duration::from_millis(100));
    }

    let child = {
        let app_state = app.state::<NativeAppState>();
        let mut state = app_state.inner.lock().unwrap();
        let Some(sidecar) = state.sidecar.as_mut() else {
            return;
        };
        if sidecar.pid != pid {
            return;
        }
        sidecar.child.take()
    };

    if let Some(child) = child {
        let _ = append_log(&log_path, "backend still alive after 3s; forcing kill");
        let _ = child.kill();
    }
}

fn wait_for_health(
    app: &AppHandle,
    launch_url: &str,
    expected_pid: Option<u32>,
) -> Result<(), Box<dyn Error>> {
    let client = Client::builder().timeout(Duration::from_secs(2)).build()?;
    let health_url = health_url_for(launch_url);
    let deadline = Instant::now() + HEALTH_WAIT_TIMEOUT;
    let mut last_error: Option<String> = None;

    while Instant::now() < deadline {
        if let Some(pid) = expected_pid {
            if let Some(message) = current_sidecar_failure(app, pid) {
                return Err(io::Error::other(message).into());
            }
        }

        match client.get(&health_url).send() {
            Ok(response) if response.status().is_success() => return Ok(()),
            Ok(response) => {
                last_error = Some(format!("health check returned {}", response.status()));
            }
            Err(err) => {
                last_error = Some(err.to_string());
            }
        }
        thread::sleep(HEALTH_POLL_INTERVAL);
    }

    Err(io::Error::other(
        last_error.unwrap_or_else(|| format!("timed out waiting for {health_url}")),
    )
    .into())
}

fn current_sidecar_failure(app: &AppHandle, pid: u32) -> Option<String> {
    let app_state = app.state::<NativeAppState>();
    let state = app_state.inner.lock().unwrap();
    let sidecar = state.sidecar.as_ref()?;
    if sidecar.pid != pid {
        return None;
    }
    if sidecar.child.is_none() {
        return sidecar.last_termination.clone();
    }
    None
}

fn current_log_path(app: &AppHandle) -> io::Result<PathBuf> {
    let app_state = app.state::<NativeAppState>();
    let state = app_state.inner.lock().unwrap();
    if let Some(sidecar) = state.sidecar.as_ref() {
        return Ok(sidecar.log_path.clone());
    }
    new_log_path()
}

fn new_log_path() -> io::Result<PathBuf> {
    let home = env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| io::Error::other("HOME is not set"))?;
    let log_dir = home.join("Library/Logs/Wiki");
    fs::create_dir_all(&log_dir)?;

    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    Ok(log_dir.join(format!("wiki-backend-{stamp}.log")))
}

fn append_log(log_path: &Path, line: &str) -> io::Result<()> {
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)?;
    writeln!(file, "{line}")?;
    Ok(())
}

fn set_app_origin(app: &AppHandle, launch_url: &str) {
    let origin = Url::parse(launch_url)
        .ok()
        .map(|url| url.origin().ascii_serialization());
    {
        let app_state = app.state::<NativeAppState>();
        let mut state = app_state.inner.lock().unwrap();
        state.app_origin = origin.clone();
    }
    if let Err(err) = register_wiki_app_secret_capability(app, launch_url) {
        eprintln!("failed to register get_wiki_app_secret capability for {launch_url}: {err}");
    }
}

/// Register a narrowly-scoped remote ACL capability that grants the main
/// webview permission to invoke `get_wiki_app_secret` when its document
/// origin exactly matches the sidecar loopback origin. Tauri 2.11 treats
/// `http://127.0.0.1:PORT/` as a remote origin, so the compiled-in
/// `default.json` capability (local-only, no remote URLs) does not
/// authorize any custom commands from that document — the invoke silently
/// fails and `/spawn`/`/gate` cannot obtain the secret. Registering here
/// (rather than in `default.json`) avoids wildcard `remote.urls` and pins
/// authorization to the exact scheme+host+port picked at sidecar boot;
/// the pattern `<scheme>://<host>[:port]/*` matches every path under
/// that single loopback origin and nothing else. WIKI-148 round 7.
fn register_wiki_app_secret_capability<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    launch_url: &str,
) -> Result<(), Box<dyn Error>> {
    let parsed =
        Url::parse(launch_url).map_err(|err| format!("invalid launch url {launch_url}: {err}"))?;
    let origin = parsed.origin().ascii_serialization();
    // urlpattern-style: origin + wildcard pathname keeps this scoped to
    // the exact scheme/host/port while allowing the SPA to navigate
    // between paths. This is host+port specificity, NOT a `http://*`
    // wildcard.
    let url_pattern = format!("{origin}/*");
    {
        let app_state = app.state::<NativeAppState>();
        let mut state = app_state.inner.lock().unwrap();
        if !state.ipc_authorized_origins.insert(origin.clone()) {
            return Ok(());
        }
    }
    let identifier = format!(
        "wiki-app-secret-loopback-{}",
        origin
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
            .collect::<String>()
    );
    let capability = CapabilityBuilder::new(identifier)
        .local(false)
        .remote(url_pattern)
        .window(MAIN_WINDOW_LABEL)
        .permission("allow-get-wiki-app-secret");
    app.add_capability(capability)?;
    Ok(())
}

fn app_origin(app: &AppHandle) -> Option<String> {
    let app_state = app.state::<NativeAppState>();
    let state = app_state.inner.lock().unwrap();
    state.app_origin.clone()
}

/// Return the Wiki.app origin secret captured during backend startup.
/// Called by the webview via `invoke("get_wiki_app_secret")` to attach an
/// `X-Wiki-App-Secret` header on composer requests. WIKI-148 round 6, Path B.
#[tauri::command]
pub fn get_wiki_app_secret(state: tauri::State<'_, NativeAppState>) -> Result<String, String> {
    refresh_daemon_secret(&state);
    let guard = state.inner.lock().unwrap();
    guard
        .wiki_app_secret
        .clone()
        .ok_or_else(|| "wiki-app origin secret not yet captured".to_string())
}

fn allow_in_webview(app: &AppHandle, url: &Url) -> bool {
    match url.scheme() {
        "tauri" | "asset" | "about" => true,
        "http" | "https" => {
            app_origin(app).is_some_and(|origin| origin == url.origin().ascii_serialization())
        }
        _ => false,
    }
}

fn should_open_externally(app: &AppHandle, url: &Url) -> bool {
    matches!(url.scheme(), "http" | "https") && !allow_in_webview(app, url)
}

fn handle_navigation_request(app: &AppHandle, url: &Url) -> bool {
    if should_open_externally(app, url) {
        let _ = app.opener().open_url(url.as_str(), None::<&str>);
        return false;
    }
    allow_in_webview(app, url)
}

fn handle_new_window_request(app: &AppHandle, url: &Url) -> NewWindowResponse<tauri::Wry> {
    if should_open_externally(app, url) {
        let _ = app.opener().open_url(url.as_str(), None::<&str>);
        return NewWindowResponse::Deny;
    }
    if allow_in_webview(app, url) {
        return NewWindowResponse::Allow;
    }
    NewWindowResponse::Deny
}

fn normalize_launch_url(raw: &str) -> String {
    let trimmed = raw.trim();
    if let Ok(mut url) = reqwest::Url::parse(trimmed) {
        if url.path().is_empty() {
            url.set_path("/");
        }
        return url.to_string();
    }

    if trimmed.ends_with('/') {
        trimmed.to_string()
    } else {
        format!("{trimmed}/")
    }
}

fn health_url_for(launch_url: &str) -> String {
    if let Ok(mut url) = reqwest::Url::parse(launch_url) {
        url.set_fragment(None);
        url.set_query(None);
        url.set_path("/");
        if let Ok(health) = url.join("health") {
            return health.to_string();
        }
    }

    let trimmed = launch_url.trim().trim_end_matches('/');
    format!("{trimmed}/health")
}

fn pick_loopback_port() -> io::Result<u16> {
    for candidate in preferred_ports() {
        if TcpListener::bind(("127.0.0.1", candidate)).is_ok() {
            return Ok(candidate);
        }
    }
    let listener = TcpListener::bind(("127.0.0.1", 0))?;
    Ok(listener.local_addr()?.port())
}

fn preferred_ports() -> Vec<u16> {
    let mut ports = Vec::new();
    if let Ok(raw) = env::var("WIKI_NATIVE_PORT") {
        if let Ok(parsed) = raw.trim().parse::<u16>() {
            if parsed != 0 {
                ports.push(parsed);
            }
        }
    }
    if !ports.contains(&DEFAULT_LOOPBACK_PORT) {
        ports.push(DEFAULT_LOOPBACK_PORT);
    }
    ports
}

fn resolve_repo_dir() -> PathBuf {
    if let Some(value) = env::var_os("WIKI_NATIVE_REPO_DIR") {
        return PathBuf::from(value);
    }

    repo_dir_from_manifest_dir(Path::new(env!("CARGO_MANIFEST_DIR")))
}

fn repo_dir_from_manifest_dir(manifest_dir: &Path) -> PathBuf {
    let repo_dir = manifest_dir.parent().unwrap().to_path_buf();
    for ancestor in repo_dir.ancestors() {
        let name = ancestor.file_name();
        if name == Some(OsStr::new(".codex")) || name == Some(OsStr::new(".native-build-staging")) {
            if let Some(parent) = ancestor.parent() {
                return parent.to_path_buf();
            }
        }
    }
    repo_dir
}

fn resolve_vault_dir(repo_dir: &Path) -> PathBuf {
    if let Some(value) = env::var_os("WIKI_NATIVE_VAULT_DIR") {
        return PathBuf::from(value);
    }
    repo_dir.join("vault")
}

fn decode_line(line: &[u8]) -> String {
    String::from_utf8_lossy(line).trim_end().to_string()
}

fn termination_summary(payload: &TerminatedPayload) -> String {
    match (payload.code, payload.signal) {
        (Some(code), Some(signal)) => format!("code={code} signal={signal}"),
        (Some(code), None) => format!("code={code}"),
        (None, Some(signal)) => format!("signal={signal}"),
        (None, None) => "unknown".to_string(),
    }
}

fn show_error_dialog(app: &AppHandle, title: &str, message: &str) {
    app.dialog()
        .message(message.to_string())
        .title(title)
        .kind(MessageDialogKind::Error)
        .blocking_show();
}

#[cfg(unix)]
fn request_graceful_shutdown(pid: u32) {
    unsafe {
        libc::kill(pid as i32, libc::SIGTERM);
    }
}

#[cfg(not(unix))]
fn request_graceful_shutdown(_pid: u32) {}

#[cfg(test)]
mod tests {
    use super::repo_dir_from_manifest_dir;
    use std::path::Path;

    #[test]
    fn sidecar_environment_excludes_origin_secret() {
        assert!(super::sidecar_environment_without_secret()
            .iter()
            .all(|(key, _)| key != "WIKI_APP_SECRET"));
    }

    #[test]
    fn native_dev_bypasses_installed_daemon_probe() {
        assert!(super::native_dev_flag(Some("1")));
        assert!(!super::native_dev_flag(Some("0")));
        assert!(!super::native_dev_flag(None));
    }

    #[test]
    fn repo_dir_defaults_to_manifest_parent() {
        assert_eq!(
            repo_dir_from_manifest_dir(Path::new("/Users/henry/me/fun/wiki/src-tauri")),
            Path::new("/Users/henry/me/fun/wiki")
        );
    }

    #[test]
    fn repo_dir_escapes_codex_worktree() {
        assert_eq!(
            repo_dir_from_manifest_dir(Path::new(
                "/Users/henry/me/fun/wiki/.codex/worktrees/wt-1/src-tauri"
            )),
            Path::new("/Users/henry/me/fun/wiki")
        );
    }

    #[test]
    fn repo_dir_escapes_native_build_staging() {
        assert_eq!(
            repo_dir_from_manifest_dir(Path::new(
                "/Users/henry/me/fun/wiki/.native-build-staging/20260715-183651-3939/src-tauri"
            )),
            Path::new("/Users/henry/me/fun/wiki")
        );
    }

    // WIKI-148 round 7: exercise the REAL Tauri IPC + ACL path for the
    // `get_wiki_app_secret` invoke command. Playwright cannot run this
    // (no Tauri IPC transport in vanilla Chromium), so the security
    // contract is enforced here. Covers:
    //   1. A webview on the registered loopback origin can invoke the
    //      command and receives the secret.
    //   2. A webview on a different origin (arbitrary URL) is rejected
    //      by the ACL — the invoke returns an error, not the secret.
    //   3. Registering the same origin twice is idempotent (HashSet
    //      short-circuit) so sidecar re-issues of `set_app_origin` for
    //      the same URL don't spam the authority table.
    mod capability {
        use super::super::{
            get_wiki_app_secret, register_wiki_app_secret_capability, NativeAppState,
        };
        use reqwest::Url;
        use tauri::{
            ipc::{CallbackFn, InvokeBody},
            test::{get_ipc_response, mock_builder, INVOKE_KEY},
            webview::InvokeRequest,
            Manager, WebviewUrl, WebviewWindowBuilder,
        };

        const LOOPBACK_URL: &str = "http://127.0.0.1:12345/";
        const EVIL_URL: &str = "https://evil.example.com/";
        const FIXTURE_SECRET: &str = "test-wiki-app-secret";

        fn build_app_with_secret() -> tauri::App<tauri::test::MockRuntime> {
            let app = mock_builder()
                .invoke_handler(tauri::generate_handler![get_wiki_app_secret])
                .manage(NativeAppState::default())
                .build(tauri::generate_context!())
                .expect("mock app build");
            {
                let state = app.state::<NativeAppState>();
                let mut guard = state.inner.lock().unwrap();
                guard.wiki_app_secret = Some(FIXTURE_SECRET.to_string());
            }
            app
        }

        fn invoke_get_secret(
            webview: &tauri::WebviewWindow<tauri::test::MockRuntime>,
            document_url: &str,
        ) -> Result<String, serde_json::Value> {
            let request = InvokeRequest {
                cmd: "get_wiki_app_secret".into(),
                callback: CallbackFn(0),
                error: CallbackFn(1),
                url: document_url.parse().unwrap(),
                body: InvokeBody::default(),
                headers: Default::default(),
                invoke_key: INVOKE_KEY.to_string(),
            };
            get_ipc_response(webview, request).map(|body| body.deserialize().unwrap())
        }

        #[test]
        fn loopback_origin_is_authorized() {
            let app = build_app_with_secret();
            let handle = app.handle().clone();
            register_wiki_app_secret_capability(&handle, LOOPBACK_URL)
                .expect("register capability");
            let webview = WebviewWindowBuilder::new(
                &app,
                super::super::MAIN_WINDOW_LABEL,
                WebviewUrl::External(LOOPBACK_URL.parse().unwrap()),
            )
            .build()
            .expect("webview build");
            let value = invoke_get_secret(&webview, LOOPBACK_URL)
                .expect("ACL should authorize invoke from loopback origin");
            assert_eq!(value, FIXTURE_SECRET);
        }

        #[test]
        fn foreign_origin_is_rejected() {
            let app = build_app_with_secret();
            let handle = app.handle().clone();
            register_wiki_app_secret_capability(&handle, LOOPBACK_URL)
                .expect("register capability");
            let webview = WebviewWindowBuilder::new(
                &app,
                super::super::MAIN_WINDOW_LABEL,
                WebviewUrl::External(EVIL_URL.parse().unwrap()),
            )
            .build()
            .expect("webview build");
            let result = invoke_get_secret(&webview, EVIL_URL);
            assert!(
                result.is_err(),
                "expected ACL rejection for foreign origin, got {result:?}"
            );
        }

        #[test]
        fn duplicate_origin_registration_is_idempotent() {
            let app = build_app_with_secret();
            let handle = app.handle().clone();
            register_wiki_app_secret_capability(&handle, LOOPBACK_URL).unwrap();
            register_wiki_app_secret_capability(&handle, LOOPBACK_URL).unwrap();
            let state = app.state::<NativeAppState>();
            let guard = state.inner.lock().unwrap();
            let origin = Url::parse(LOOPBACK_URL)
                .unwrap()
                .origin()
                .ascii_serialization();
            assert!(guard.ipc_authorized_origins.contains(&origin));
            assert_eq!(guard.ipc_authorized_origins.len(), 1);
        }
    }
}
