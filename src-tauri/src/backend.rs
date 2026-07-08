use std::{
    env,
    error::Error,
    io, thread,
    time::{Duration, Instant},
};

use reqwest::blocking::Client;
use tauri::{AppHandle, WebviewUrl, WebviewWindowBuilder};

const HEALTH_WAIT_TIMEOUT: Duration = Duration::from_secs(15);
const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(200);

pub fn launch(app: &AppHandle) -> Result<(), Box<dyn Error>> {
    let backend_url = env::var("WIKI_NATIVE_BACKEND_URL").map_err(|_| {
        io::Error::other("WIKI_NATIVE_BACKEND_URL is required in Phase 1 manual-backend mode")
    })?;
    let launch_url = normalize_launch_url(&backend_url);
    wait_for_health(&launch_url)?;

    WebviewWindowBuilder::new(app, "main", WebviewUrl::External(launch_url.parse()?))
        .title("Wiki")
        .inner_size(1400.0, 950.0)
        .resizable(true)
        .build()?;

    Ok(())
}

fn normalize_launch_url(raw: &str) -> String {
    let trimmed = raw.trim().trim_end_matches('/');
    format!("{trimmed}/")
}

fn wait_for_health(launch_url: &str) -> Result<(), Box<dyn Error>> {
    let client = Client::builder().timeout(Duration::from_secs(2)).build()?;
    let health_url = format!("{launch_url}health");
    let deadline = Instant::now() + HEALTH_WAIT_TIMEOUT;
    let mut last_error: Option<String> = None;

    while Instant::now() < deadline {
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
