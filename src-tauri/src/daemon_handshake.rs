use std::{
    io::{self, Read},
    os::unix::net::UnixStream,
    path::{Path, PathBuf},
    time::Duration,
};

pub(crate) const SOCKET_NAME: &str = "wiki-app-secret.sock";

pub(crate) fn socket_path(runtime_dir: &Path) -> PathBuf {
    runtime_dir.join(SOCKET_NAME)
}

pub(crate) fn read_secret(runtime_dir: &Path) -> io::Result<String> {
    let mut stream = UnixStream::connect(socket_path(runtime_dir))?;
    stream.set_read_timeout(Some(Duration::from_millis(350)))?;
    let mut contents = String::new();
    stream.read_to_string(&mut contents)?;
    let secret = contents.trim().to_string();
    if secret.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "daemon app secret handshake was empty",
        ));
    }
    Ok(secret)
}
