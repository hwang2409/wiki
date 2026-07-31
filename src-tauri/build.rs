use std::{env, fs, path::PathBuf};

use sha2::{Digest, Sha256};

fn source_backend_fingerprint() -> String {
    let root = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap()).join("..");
    let runtime = root.join("backend/app/agent_runtime");
    let app = root.join("backend/app");
    let mut sources: Vec<PathBuf> = fs::read_dir(&runtime)
        .expect("backend runtime directory is missing")
        .map(|entry| entry.expect("backend runtime entry is unreadable").path())
        .filter(|path| path.extension().is_some_and(|ext| ext == "py"))
        .collect();
    sources.extend(
        fs::read_dir(&app)
            .expect("backend app directory is missing")
            .map(|entry| entry.expect("backend app entry is unreadable").path())
            .filter(|path| {
                path.file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| name.starts_with("knowledge") && name.ends_with(".py"))
            }),
    );
    sources.push(app.join("wiki_artifacts.py"));
    sources.sort();

    let mut digest = Sha256::new();
    for path in sources {
        digest.update(path.file_name().unwrap().to_string_lossy().as_bytes());
        digest.update(fs::read(path).expect("backend source is unreadable"));
    }
    format!("{:x}", digest.finalize())
}

fn main() {
    let fingerprint = env::var("WIKI_EXPECTED_BACKEND_FINGERPRINT")
        .unwrap_or_else(|_| source_backend_fingerprint());
    println!("cargo:rustc-env=WIKI_EXPECTED_BACKEND_FINGERPRINT={fingerprint}");
    println!("cargo:rerun-if-env-changed=WIKI_EXPECTED_BACKEND_FINGERPRINT");

    // App-command manifest: autogenerates `allow-get-wiki-app-secret` /
    // `deny-get-wiki-app-secret` permissions for the `get_wiki_app_secret`
    // invoke command so runtime capabilities (see
    // `register_wiki_app_secret_capability` in `backend.rs`) can reference it.
    // WIKI-148 round 7: without the manifest, Tauri 2.11's ACL rejects the
    // invoke from the loopback webview.
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(&["get_wiki_app_secret"])),
    )
    .expect("failed to run tauri-build");
}
