fn main() {
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
