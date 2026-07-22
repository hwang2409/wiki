#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend;

// The Tauri app entry point embeds resources (`generate_context!`) that can
// only be linked once per binary. Tests inside the `backend` module also
// call `generate_context!` — gating `main` out of the test build keeps the
// resource symbols single-defined. WIKI-148 round 7.
#[cfg(not(test))]
fn main() {
    let app_lock = match backend::acquire_app_lock() {
        Ok(lock) => lock,
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            backend::show_already_running_dialog();
            return;
        }
        Err(error) => {
            eprintln!("cannot start Wiki: {error}");
            return;
        }
    };

    let result = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![backend::get_wiki_app_secret])
        .setup(move |app| backend::setup(app, app_lock))
        .on_window_event(backend::handle_window_event)
        .build(tauri::generate_context!());

    match result {
        Ok(app) => app.run(backend::handle_run_event),
        Err(error) => eprintln!("error while building wiki native shell: {error}"),
    }
}
