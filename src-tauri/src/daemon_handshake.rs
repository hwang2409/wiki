use std::{
    fs,
    io::{self, Read},
    os::{fd::AsRawFd, unix::net::UnixStream},
    path::{Path, PathBuf},
    time::Duration,
};

use sha2::{Digest, Sha256};

pub(crate) const SOCKET_NAME: &str = "wiki-app-secret.sock";
const MAX_SECRET_BYTES: usize = 256;

pub(crate) struct AuthenticatedSecret {
    pub(crate) secret: String,
    pub(crate) pid: u32,
}

pub(crate) fn socket_path(runtime_dir: &Path) -> PathBuf {
    runtime_dir.join(SOCKET_NAME)
}

pub(crate) fn read_secret(runtime_dir: &Path) -> io::Result<String> {
    let mut stream = UnixStream::connect(socket_path(runtime_dir))?;
    stream.set_read_timeout(Some(Duration::from_millis(350)))?;
    read_handshake_secret(&mut stream)
}

pub(crate) fn read_authenticated_secret(
    runtime_dir: &Path,
    expected_executable: &Path,
    expected_fingerprint: &str,
) -> io::Result<AuthenticatedSecret> {
    let mut stream = UnixStream::connect(socket_path(runtime_dir))?;
    stream.set_read_timeout(Some(Duration::from_millis(350)))?;
    let pid = peer_pid(&stream)?;
    let executable = process_executable(pid)?;
    if !same_file(&executable, expected_executable) {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "daemon auth socket peer executable is not the selected backend",
        ));
    }
    if fingerprint(&executable)? != expected_fingerprint {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "daemon auth socket peer fingerprint does not match",
        ));
    }
    let secret = read_handshake_secret(&mut stream)?;
    Ok(AuthenticatedSecret { secret, pid })
}

fn read_handshake_secret(stream: &mut UnixStream) -> io::Result<String> {
    let mut bytes = Vec::with_capacity(MAX_SECRET_BYTES);
    let mut byte = [0_u8; 1];
    loop {
        match stream.read(&mut byte)? {
            0 => break,
            1 if byte[0] == b'\n' => break,
            1 => {
                if bytes.len() >= MAX_SECRET_BYTES {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "daemon app secret handshake was too long",
                    ));
                }
                bytes.push(byte[0]);
            }
            _ => unreachable!("one-byte handshake read returned more than one byte"),
        }
    }
    let secret = String::from_utf8(bytes)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?
        .trim()
        .to_string();
    if secret.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "daemon app secret handshake was empty",
        ));
    }

    match stream.read(&mut byte) {
        Ok(0) => Ok(secret),
        Ok(_) => Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "daemon app secret handshake had extra bytes",
        )),
        Err(error) if error.kind() == io::ErrorKind::TimedOut => Ok(secret),
        Err(error) => Err(error),
    }
}

#[cfg(test)]
mod tests {
    use super::{read_secret, socket_path, MAX_SECRET_BYTES};
    use std::{fs, io::Write, os::unix::net::UnixListener, path::PathBuf, thread};

    #[test]
    fn streaming_peer_is_rejected_without_waiting_for_eof() {
        let runtime = PathBuf::from(format!("/tmp/wiki-secret-{}", std::process::id()));
        let _ = fs::remove_dir_all(&runtime);
        fs::create_dir_all(&runtime).unwrap();
        let listener = UnixListener::bind(socket_path(&runtime)).unwrap();
        let writer = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            for _ in 0..=MAX_SECRET_BYTES {
                if stream.write_all(b"x").is_err() {
                    break;
                }
            }
            let _ = stream.write_all(b"never reaches eof");
        });

        let error = read_secret(&runtime).unwrap_err();
        assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
        assert!(error.to_string().contains("too long"));
        writer.join().unwrap();
        let _ = fs::remove_file(PathBuf::from(socket_path(&runtime)));
        fs::remove_dir_all(runtime).unwrap();
    }
}

fn peer_pid(stream: &UnixStream) -> io::Result<u32> {
    #[cfg(target_os = "macos")]
    let (level, option) = (0, 0x002);
    #[cfg(target_os = "linux")]
    let (level, option) = (libc::SOL_SOCKET, libc::SO_PEERCRED);
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    return Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "peer PID lookup is unsupported on this platform",
    ));

    #[cfg(any(target_os = "macos", target_os = "linux"))]
    unsafe {
        #[cfg(target_os = "linux")]
        let mut credentials = libc::ucred {
            pid: 0,
            uid: 0,
            gid: 0,
        };
        #[cfg(target_os = "macos")]
        let mut credentials: libc::pid_t = 0;
        let credentials_ptr: *mut libc::c_void = std::ptr::addr_of_mut!(credentials).cast();
        let mut length = std::mem::size_of_val(&credentials) as libc::socklen_t;
        let result = libc::getsockopt(
            stream.as_raw_fd(),
            level,
            option,
            credentials_ptr,
            &mut length,
        );
        if result != 0 {
            return Err(io::Error::last_os_error());
        }
        #[cfg(target_os = "linux")]
        let pid = credentials.pid;
        #[cfg(target_os = "macos")]
        let pid = credentials;
        if pid <= 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "daemon auth socket returned an invalid peer PID",
            ));
        }
        Ok(pid as u32)
    }
}

fn process_executable(pid: u32) -> io::Result<PathBuf> {
    #[cfg(target_os = "linux")]
    {
        return fs::read_link(format!("/proc/{pid}/exe"));
    }
    #[cfg(target_os = "macos")]
    {
        let mut buffer = vec![0_u8; 4096];
        let size = unsafe {
            proc_pidpath(
                pid as libc::pid_t,
                buffer.as_mut_ptr().cast(),
                buffer.len() as u32,
            )
        };
        if size <= 0 {
            return Err(io::Error::last_os_error());
        }
        buffer.truncate(size as usize);
        return String::from_utf8(buffer)
            .map(PathBuf::from)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error));
    }
    #[allow(unreachable_code)]
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "process executable lookup is unsupported on this platform",
    ))
}

fn same_file(first: &Path, second: &Path) -> bool {
    let Ok(first_canonical) = first.canonicalize() else {
        return false;
    };
    let Ok(second_canonical) = second.canonicalize() else {
        return false;
    };
    if first_canonical != second_canonical {
        return false;
    }
    let Ok(first_metadata) = fs::metadata(first) else {
        return false;
    };
    let Ok(second_metadata) = fs::metadata(second) else {
        return false;
    };
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        return first_metadata.dev() == second_metadata.dev()
            && first_metadata.ino() == second_metadata.ino();
    }
    #[allow(unreachable_code)]
    true
}

fn fingerprint(path: &Path) -> io::Result<String> {
    let digest = Sha256::digest(fs::read(path)?);
    Ok(format!("{digest:x}"))
}

pub(crate) fn hmac_sha256_hex(secret: &str, message: &str) -> String {
    const BLOCK_SIZE: usize = 64;
    let mut key = [0_u8; BLOCK_SIZE];
    let secret_bytes = secret.as_bytes();
    if secret_bytes.len() > BLOCK_SIZE {
        let digest = Sha256::digest(secret_bytes);
        key[..digest.len()].copy_from_slice(&digest);
    } else {
        key[..secret_bytes.len()].copy_from_slice(secret_bytes);
    }
    let mut inner_pad = [0x36_u8; BLOCK_SIZE];
    let mut outer_pad = [0x5c_u8; BLOCK_SIZE];
    for index in 0..BLOCK_SIZE {
        inner_pad[index] ^= key[index];
        outer_pad[index] ^= key[index];
    }
    let mut inner = Sha256::new();
    inner.update(inner_pad);
    inner.update(message.as_bytes());
    let inner_digest = inner.finalize();
    let mut outer = Sha256::new();
    outer.update(outer_pad);
    outer.update(inner_digest);
    format!("{:x}", outer.finalize())
}

#[cfg(target_os = "macos")]
unsafe extern "C" {
    fn proc_pidpath(pid: libc::pid_t, buffer: *mut libc::c_void, buffersize: u32) -> i32;
}
