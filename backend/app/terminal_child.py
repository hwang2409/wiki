"""Exec an interactive shell with the PTY slave as its controlling terminal."""

from __future__ import annotations

import fcntl
import os
import sys
import termios


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: terminal_child.py SHELL CWD")

    shell_path, cwd = sys.argv[1:]
    tty_fd = 0
    fcntl.ioctl(tty_fd, termios.TIOCSCTTY, 0)
    for fd in (1, 2):
        os.dup2(tty_fd, fd)
    os.chdir(cwd)
    os.execve(shell_path, [shell_path, "-il"], os.environ)


if __name__ == "__main__":
    main()
