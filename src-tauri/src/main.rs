#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend;

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .setup(backend::setup)
        .on_window_event(backend::handle_window_event)
        .build(tauri::generate_context!())
        .expect("error while building wiki native shell")
        .run(backend::handle_run_event);
}
