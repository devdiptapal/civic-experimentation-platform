"""Small, dependable file primitives shared by the workspace and the server.

Everything the platform stores is a plain file an agency can open without
this software: JSON for settings and accounts, JSON Lines for events and
audit trails. Two things make plain files safe to share between a running
server and someone using the command line at the same time:

* **Writes that replace a file are atomic.** A settings or accounts file is
  written to a temporary file and moved into place, so a reader never sees
  half of one and a crash never leaves a truncated one.
* **Appends and read-modify-write cycles take a file lock.** An audit chain
  appended by two processes at once would fork; the lock serializes them.
  ``fcntl`` is used on Linux and macOS and ``msvcrt`` on Windows, both in
  the standard library.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:  # pragma: no cover - platform dependent
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

__all__ = ["atomic_write_text", "file_lock", "read_json", "write_json", "tail_line"]

# One in-process lock per lock file. File locks alone are enough between
# processes, but threads in one server share the process, and taking the
# thread lock first keeps them from contending on the file descriptor.
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(key: str) -> threading.RLock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = _THREAD_LOCKS[key] = threading.RLock()
        return lock


@contextlib.contextmanager
def file_lock(path: str | os.PathLike[str]) -> Iterator[None]:
    """Hold an exclusive lock associated with ``path`` for the block.

    The lock lives in a hidden sibling file, so locking never touches the
    data file itself and works whether or not it exists yet.
    """
    target = Path(path)
    lock_path = target.with_name(f".{target.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock(str(lock_path.resolve())), open(lock_path, "a+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - Windows
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - no locking primitive available
            yield


def atomic_write_text(
    path: str | os.PathLike[str], text: str, *, private: bool = False
) -> None:
    """Replace ``path`` with ``text`` so readers see the old or new file, never half.

    ``private`` restricts the file to its owner, for files holding password
    hashes or key digests.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if private:
            os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def read_json(path: str | os.PathLike[str], default: Any = None) -> Any:
    target = Path(path)
    if not target.exists():
        return default
    return json.loads(target.read_text(encoding="utf-8"))


def write_json(path: str | os.PathLike[str], data: Any, *, private: bool = False) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n", private=private)


def tail_line(path: str | os.PathLike[str], *, max_bytes: int = 65536) -> str | None:
    """The last complete line of a file, read without loading the whole file.

    Used to find when a pilot last received data, which the monitor checks
    on every running evaluation and should not cost a full read of a large
    event log.
    """
    target = Path(path)
    try:
        size = target.stat().st_size
    except FileNotFoundError:
        return None
    if size == 0:
        return None
    with target.open("rb") as handle:
        handle.seek(max(0, size - max_bytes))
        chunk = handle.read()
    lines = [line for line in chunk.split(b"\n") if line.strip()]
    if not lines:
        return None
    return lines[-1].decode("utf-8", errors="replace")
