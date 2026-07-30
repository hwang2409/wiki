"""Byte-bounded, resumable line reader + fd-based child opens.

The reader owns its own position, buffered partial record, and
mid-oversized-record ``skipping`` flag. A caller that stops mid-stream
extracts a ``resume_state`` and hands it into the next reader instance
via the cursor. That is what makes pagination survive a scan-budget cut
in the middle of an oversized record.
"""

from __future__ import annotations

import os
import stat
from typing import Iterator

from ..pathwalk import open_relative_directory, open_relative_file
from . import errors as e
from .errors import ReplayError


def _open_run_child_fd(runs_root_fd: int, run_id: str, filename: str) -> int:
    """Open ``<runs_root>/<run_id>/<filename>`` via the dir-fd walker.

    ``open_relative_file`` refuses a symlink at ANY component. We pass
    ``O_NONBLOCK`` on the final open so a FIFO or device swapped in for
    a regular file returns immediately instead of blocking the request
    in the kernel. After the open we ``fstat`` and refuse anything that
    isn't a regular file — a FIFO opened with O_NONBLOCK would still let
    us read junk, and a device could give the client arbitrary system
    state.
    """

    o_nonblock = getattr(os, "O_NONBLOCK", 0)
    try:
        fd = open_relative_file(
            runs_root_fd, (run_id, filename), extra_final_flags=o_nonblock
        )
    except FileNotFoundError as exc:
        raise ReplayError(
            f"{filename} not found for run {run_id}", status_code=404
        ) from exc
    except OSError as exc:
        raise ReplayError(
            f"{filename} not accessible for run {run_id}: {exc}",
            status_code=404,
        ) from exc
    try:
        info = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise ReplayError(
            f"could not stat {filename}: {exc}", status_code=404
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise ReplayError(
            f"{filename} is not a regular file", status_code=404
        )
    return fd


def open_runs_root_fd(runs_root_path: str | os.PathLike[str]) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    return os.open(runs_root_path, os.O_RDONLY | no_follow | directory_flag)


def verify_run_dir_exists(runs_root_fd: int, run_id: str) -> None:
    """Cheap early-existence check that mirrors production behavior."""

    try:
        fd = open_relative_directory(runs_root_fd, (run_id,))
    except FileNotFoundError as exc:
        raise ReplayError("run not found", status_code=404) from exc
    except OSError as exc:
        raise ReplayError(f"run not readable: {exc}", status_code=404) from exc
    os.close(fd)


class SnapshotReader:
    """Byte-bounded, resumable line reader for a size-snapshotted fd.

    Bounds enforced:

    * Every byte physically read counts against ``max_scan_bytes`` —
      including bytes consumed while skipping an oversized record.
    * Records over ``max_line_bytes`` are dropped by advancing through
      the stream until the terminating newline; bytes are NEVER
      buffered past the ceiling.
    * Records after a skipped oversized line are preserved.
    """

    def __init__(
        self,
        fd: int,
        *,
        start_offset: int,
        start_skipping: bool,
        max_scan_bytes: int,
        max_line_bytes: int,
        stats,
    ) -> None:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ReplayError("expected a regular file", status_code=404)
        self.fd = fd
        self.snapshot_size = int(info.st_size)
        # Cursors must never point past the snapshot the SERVER just took.
        # A legitimately-signed cursor from a prior request cannot point
        # past this request's snapshot; if it does, the client tampered
        # or the file shrank between requests. Either way, 400.
        if start_offset > self.snapshot_size:
            raise ReplayError("cursor beyond end of stream", status_code=400)
        self.max_scan_bytes = max_scan_bytes
        self.max_line_bytes = max_line_bytes
        self.stats = stats
        self.buffer = bytearray()
        self.accumulated_line_bytes = 0
        self.skipping = bool(start_skipping)
        clamped_offset = start_offset
        if clamped_offset < 0:
            clamped_offset = 0
        self.position = clamped_offset
        if self.position > 0:
            os.lseek(fd, self.position, os.SEEK_SET)
        self._total_bytes_read = 0

    def resume_state(self) -> tuple[int, bool]:
        """Return ``(byte_offset, skipping)`` to encode into the next cursor."""

        if not self.skipping and self.buffer:
            return (self.position - len(self.buffer), False)
        return (self.position, self.skipping)

    def records(self) -> Iterator[tuple[int, bytes]]:
        while self.position < self.snapshot_size:
            budget_remaining = self.max_scan_bytes - self._total_bytes_read
            if budget_remaining <= 0:
                self.stats.scan_truncated = True
                return
            to_read = min(
                e.STREAM_CHUNK,
                self.snapshot_size - self.position,
                budget_remaining,
            )
            chunk = os.read(self.fd, to_read)
            if not chunk:
                break
            chunk_start = self.position
            self.position += len(chunk)
            self._total_bytes_read += len(chunk)
            offset = 0
            while offset < len(chunk):
                nl = chunk.find(b"\n", offset)
                if nl < 0:
                    slice_len = len(chunk) - offset
                    self.accumulated_line_bytes += slice_len
                    if (
                        not self.skipping
                        and self.accumulated_line_bytes > self.max_line_bytes
                    ):
                        self.stats.dropped_oversize += 1
                        self.skipping = True
                        self.buffer.clear()
                    if not self.skipping:
                        self.buffer.extend(chunk[offset:])
                    offset = len(chunk)
                    continue
                record_end_position = chunk_start + nl + 1
                if self.skipping:
                    self.skipping = False
                    self.accumulated_line_bytes = 0
                    offset = nl + 1
                    continue
                slice_len = nl - offset
                self.accumulated_line_bytes += slice_len
                if self.accumulated_line_bytes > self.max_line_bytes:
                    self.stats.dropped_oversize += 1
                    self.buffer.clear()
                    self.accumulated_line_bytes = 0
                    offset = nl + 1
                    continue
                if self.buffer:
                    self.buffer.extend(chunk[offset:nl])
                    record = bytes(self.buffer)
                    self.buffer.clear()
                else:
                    record = bytes(chunk[offset:nl])
                self.accumulated_line_bytes = 0
                offset = nl + 1
                yield record_end_position, record
            if self._total_bytes_read >= self.max_scan_bytes:
                if self.position < self.snapshot_size:
                    self.stats.scan_truncated = True
                return
        if self.buffer or self.skipping:
            self.stats.dropped_truncated_tail = True
