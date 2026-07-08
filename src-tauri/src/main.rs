#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend;

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| backend::launch(app.handle()))
        .run(tauri::generate_context!())
        .expect("error while running wiki native shell");
}
