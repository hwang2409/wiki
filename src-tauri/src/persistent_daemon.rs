use std::{
    env,
    path::{Path, PathBuf},
    process::Command,
    thread,
    time::{Duration, Instant},
};

use reqwest::blocking::Client;

use crate::daemon_handshake;

const DEFAULT_DAEMON_URL: &str = "http://127.0.0.1:8213/";
const DEFAULT_DAEMON_LABEL: &str = "com.hwang2409.wiki.backend";
const DAEMON_PROBE_TIMEOUT: Duration = Duration::from_millis(350);
const DAEMON_PROBE_WAIT_TIMEOUT: Duration = Duration::from_secs(15);
const DAEMON_PROBE_POLL_INTERVAL: Duration = Duration::from_millis(50);

pub(crate) fn probe(
    runtime_dir: &Path,
    expected_fingerprint: &str,
) -> Result<Option<(String, String)>, String> {
    if !daemon_may_be_starting(runtime_dir) {
        return Ok(None);
    }
    let launch_url = normalize_launch_url(
        &env::var("WIKI_DAEMON_BACKEND_URL").unwrap_or_else(|_| DEFAULT_DAEMON_URL.to_string()),
    );
    let health_url = health_url_for(&launch_url);
    let client = Client::builder()
        .timeout(DAEMON_PROBE_TIMEOUT)
        .build()
        .map_err(|error| format!("cannot create daemon health client: {error}"))?;
    let ready = retry_until_ready(
        || {
            let response = client.get(&health_url).send().ok()?;
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
            daemon_handshake::read_secret(runtime_dir).ok()
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

pub(crate) fn refresh_secret(runtime_dir: &Path, daemon_managed: bool) -> Option<String> {
    daemon_managed
        .then(|| daemon_handshake::read_secret(runtime_dir).ok())
        .flatten()
}

fn daemon_may_be_starting(runtime_dir: &Path) -> bool {
    daemon_probe_should_wait(
        daemon_launchd_job_loaded(),
        daemon_socket_is_live(runtime_dir),
        daemon_plist_exists(),
    )
}

fn daemon_launchd_job_loaded() -> Option<bool> {
    let target = format!("gui/{}/{}", unsafe { libc::getuid() }, DEFAULT_DAEMON_LABEL);
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

fn daemon_socket_is_live(runtime_dir: &Path) -> bool {
    let path = daemon_handshake::socket_path(runtime_dir);
    path.exists() && daemon_handshake::read_secret(runtime_dir).is_ok()
}

fn daemon_plist_exists() -> bool {
    let launch_agents = env::var_os("WIKI_LAUNCH_AGENTS_DIR")
        .map(PathBuf::from)
        .or_else(|| {
            env::var_os("HOME").map(|home| PathBuf::from(home).join("Library/LaunchAgents"))
        });
    launch_agents
        .map(|directory| {
            directory
                .join(format!("{DEFAULT_DAEMON_LABEL}.plist"))
                .is_file()
        })
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
    use super::{daemon_probe_should_wait, retry_until_ready};

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
}
