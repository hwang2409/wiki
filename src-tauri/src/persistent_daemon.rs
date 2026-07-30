use std::{
    env,
    fs::File,
    io::Read,
    path::{Path, PathBuf},
    process::Command,
    thread,
    time::{Duration, Instant},
};

use reqwest::blocking::Client;

use crate::daemon_handshake;

const DEFAULT_DAEMON_LABEL: &str = "com.hwang2409.wiki.backend";
const DEFAULT_DAEMON_PORT: u16 = 8213;
const DAEMON_SETTINGS_NAME: &str = "daemon-settings.json";
const DAEMON_PROBE_TIMEOUT: Duration = Duration::from_millis(350);
const DAEMON_PROBE_WAIT_TIMEOUT: Duration = Duration::from_secs(15);
const DAEMON_PROBE_POLL_INTERVAL: Duration = Duration::from_millis(50);

pub(crate) fn probe(
    runtime_dir: &Path,
    expected_fingerprint: &str,
) -> Result<Option<(String, String)>, String> {
    let (label, port) = daemon_connection_settings(runtime_dir)?;
    if !daemon_may_be_starting(runtime_dir, &label) {
        return Ok(None);
    }
    let launch_url = normalize_launch_url(
        &env::var("WIKI_DAEMON_BACKEND_URL")
            .unwrap_or_else(|_| format!("http://127.0.0.1:{port}/")),
    );
    let health_url = health_url_for(&launch_url);
    let client = Client::builder()
        .timeout(DAEMON_PROBE_TIMEOUT)
        .build()
        .map_err(|error| format!("cannot create daemon health client: {error}"))?;
    let ready = retry_until_ready(
        || {
            let expected_executable = expected_backend_executable()?;
            let authenticated = daemon_handshake::read_authenticated_secret(
                runtime_dir,
                &expected_executable,
                expected_fingerprint,
            )
            .ok()?;
            let nonce = health_nonce().ok()?;
            let response = client
                .get(&health_url)
                .header("X-Wiki-Daemon-Nonce", &nonce)
                .send()
                .ok()?;
            if !response.status().is_success() {
                return None;
            }
            let payload = response.json::<serde_json::Value>().ok()?;
            if payload
                .get("daemon_managed")
                .and_then(serde_json::Value::as_bool)
                != Some(true)
            {
                return None;
            }
            if payload
                .get("backend_fingerprint")
                .and_then(serde_json::Value::as_str)
                != Some(expected_fingerprint)
            {
                return None;
            }
            if !health_matches_authenticated_peer(&payload, authenticated.pid, expected_fingerprint)
                || !health_proof_matches(&payload, &authenticated.secret, &nonce)
                || daemon_launchd_pid(&label) != Some(authenticated.pid)
            {
                return None;
            }
            Some(authenticated.secret)
        },
        DAEMON_PROBE_WAIT_TIMEOUT,
        DAEMON_PROBE_POLL_INTERVAL,
    );
    ready
        .map(|secret| Ok(Some((launch_url, secret))))
        .unwrap_or_else(|| {
            Err(format!(
                "persistent daemon did not become ready at {health_url}"
            ))
        })
}

pub(crate) fn refresh_secret(
    runtime_dir: &Path,
    daemon_managed: bool,
    expected_fingerprint: &str,
) -> Option<String> {
    if !daemon_managed {
        return None;
    }
    let expected_executable = expected_backend_executable()?;
    let authenticated = daemon_handshake::read_authenticated_secret(
        runtime_dir,
        &expected_executable,
        expected_fingerprint,
    )
    .ok()?;
    let (label, _) = daemon_connection_settings(runtime_dir).ok()?;
    (daemon_launchd_pid(&label) == Some(authenticated.pid)).then_some(authenticated.secret)
}

fn daemon_connection_settings(runtime_dir: &Path) -> Result<(String, u16), String> {
    let path = runtime_dir.join(DAEMON_SETTINGS_NAME);
    let contents = match std::fs::read_to_string(path) {
        Ok(contents) => contents,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok((DEFAULT_DAEMON_LABEL.to_string(), DEFAULT_DAEMON_PORT));
        }
        Err(error) => return Err(format!("cannot read daemon settings: {error}")),
    };
    let value: serde_json::Value = serde_json::from_str(&contents)
        .map_err(|error| format!("invalid daemon settings: {error}"))?;
    let label = value
        .get("label")
        .and_then(serde_json::Value::as_str)
        .filter(|label| !label.is_empty())
        .unwrap_or(DEFAULT_DAEMON_LABEL)
        .to_string();
    let port = value
        .get("port")
        .and_then(serde_json::Value::as_u64)
        .and_then(|port| u16::try_from(port).ok())
        .filter(|port| *port > 0)
        .unwrap_or(DEFAULT_DAEMON_PORT);
    Ok((label, port))
}

fn expected_backend_executable() -> Option<PathBuf> {
    if let Some(path) = env::var_os("WIKI_BACKEND_EXECUTABLE") {
        return Some(PathBuf::from(path));
    }
    if let Some(app_path) = env::var_os("WIKI_APP_PATH") {
        return Some(
            PathBuf::from(app_path).join("Contents/Resources/wiki-backend-sidecar/wiki-backend"),
        );
    }
    let current = env::current_exe().ok()?;
    current
        .ancestors()
        .find(|path| path.file_name().is_some_and(|name| name == "Wiki.app"))
        .map(|app| app.join("Contents/Resources/wiki-backend-sidecar/wiki-backend"))
        .filter(|path| path.is_file())
}

fn health_matches_authenticated_peer(
    payload: &serde_json::Value,
    peer_pid: u32,
    expected_fingerprint: &str,
) -> bool {
    payload.get("process_id").and_then(|value| value.as_u64()) == Some(peer_pid as u64)
        && payload
            .get("backend_fingerprint")
            .and_then(|value| value.as_str())
            == Some(expected_fingerprint)
}

fn health_proof_matches(payload: &serde_json::Value, secret: &str, nonce: &str) -> bool {
    let Some(proof) = payload.get("daemon_proof").and_then(|value| value.as_str()) else {
        return false;
    };
    constant_time_equal(
        proof.as_bytes(),
        daemon_handshake::hmac_sha256_hex(secret, nonce).as_bytes(),
    )
}

fn constant_time_equal(first: &[u8], second: &[u8]) -> bool {
    if first.len() != second.len() {
        return false;
    }
    first
        .iter()
        .zip(second)
        .fold(0_u8, |difference, (left, right)| {
            difference | (left ^ right)
        })
        == 0
}

fn health_nonce() -> std::io::Result<String> {
    let mut bytes = [0_u8; 32];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn daemon_may_be_starting(runtime_dir: &Path, label: &str) -> bool {
    daemon_probe_should_wait(
        daemon_launchd_job_loaded(label),
        daemon_socket_is_live(runtime_dir),
        daemon_plist_exists(label),
    )
}

fn daemon_launchd_job_loaded(label: &str) -> Option<bool> {
    let target = format!("gui/{}/{}", unsafe { libc::getuid() }, label);
    let output = Command::new("launchctl")
        .args(["print", target.as_str()])
        .output()
        .ok()?;
    if output.status.success() {
        return Some(true);
    }
    if output.status.code() == Some(113) {
        return Some(false);
    }
    None
}

fn daemon_launchd_pid(label: &str) -> Option<u32> {
    let target = format!("gui/{}/{}", unsafe { libc::getuid() }, label);
    let output = Command::new("launchctl")
        .args(["print", target.as_str()])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .find_map(|line| line.trim().strip_prefix("pid = ")?.parse().ok())
}

fn daemon_socket_is_live(runtime_dir: &Path) -> bool {
    let path = daemon_handshake::socket_path(runtime_dir);
    path.exists() && daemon_handshake::read_secret(runtime_dir).is_ok()
}

fn daemon_plist_exists(label: &str) -> bool {
    let launch_agents = env::var_os("WIKI_LAUNCH_AGENTS_DIR")
        .map(PathBuf::from)
        .or_else(|| {
            env::var_os("HOME").map(|home| PathBuf::from(home).join("Library/LaunchAgents"))
        });
    launch_agents
        .map(|directory| directory.join(format!("{label}.plist")).is_file())
        .unwrap_or(false)
}

fn daemon_probe_should_wait(
    job_loaded: Option<bool>,
    socket_live: bool,
    plist_exists: bool,
) -> bool {
    job_loaded == Some(true) || socket_live || plist_exists
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

fn retry_until_ready<T, F>(mut probe: F, timeout: Duration, interval: Duration) -> Option<T>
where
    F: FnMut() -> Option<T>,
{
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(value) = probe() {
            return Some(value);
        }
        if Instant::now() >= deadline {
            return None;
        }
        thread::sleep(interval);
    }
}

#[cfg(test)]
mod tests {
    use super::{
        daemon_connection_settings, daemon_probe_should_wait, health_matches_authenticated_peer,
        health_proof_matches, retry_until_ready, DAEMON_SETTINGS_NAME,
    };

    #[test]
    fn retries_until_ready() {
        let mut attempts = 0;
        let result = retry_until_ready(
            || {
                attempts += 1;
                (attempts >= 3).then_some("ready")
            },
            std::time::Duration::from_secs(1),
            std::time::Duration::from_millis(1),
        );
        assert_eq!(result, Some("ready"));
        assert_eq!(attempts, 3);
    }

    #[test]
    fn falls_back_only_when_no_daemon_is_starting() {
        assert!(!daemon_probe_should_wait(Some(false), false, false));
        assert!(!daemon_probe_should_wait(None, false, false));
        assert!(daemon_probe_should_wait(Some(true), false, false));
        assert!(daemon_probe_should_wait(Some(false), true, false));
        assert!(daemon_probe_should_wait(Some(false), false, true));
    }

    #[test]
    fn forged_health_fails_before_the_real_peer_passes() {
        let forged = serde_json::json!({
            "process_id": 100,
            "backend_fingerprint": "expected",
        });
        let real = serde_json::json!({
            "process_id": 200,
            "backend_fingerprint": "expected",
        });
        assert!(!health_matches_authenticated_peer(&forged, 200, "expected"));
        assert!(health_matches_authenticated_peer(&real, 200, "expected"));
        assert!(!health_proof_matches(&forged, "daemon-secret", "nonce"));
        let mut authenticated = real;
        authenticated["daemon_proof"] = serde_json::Value::String(
            super::daemon_handshake::hmac_sha256_hex("daemon-secret", "nonce"),
        );
        assert!(health_proof_matches(
            &authenticated,
            "daemon-secret",
            "nonce"
        ));
    }

    #[test]
    fn probe_uses_persisted_non_default_port_and_label() {
        let runtime = std::env::temp_dir().join(format!(
            "wiki-native-daemon-settings-{}",
            std::process::id()
        ));
        std::fs::create_dir_all(&runtime).unwrap();
        std::fs::write(
            runtime.join(DAEMON_SETTINGS_NAME),
            r#"{"label":"com.example.wiki.test","port":9321}"#,
        )
        .unwrap();
        assert_eq!(
            daemon_connection_settings(&runtime).unwrap(),
            ("com.example.wiki.test".to_string(), 9321)
        );
        std::fs::remove_dir_all(runtime).unwrap();
    }
}
