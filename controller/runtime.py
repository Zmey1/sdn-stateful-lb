"""Shared lab runtime directory: atomic JSON files and process checks.

Every lab process (backends, probe, controller, CLI, dashboard, benchmarks)
exchanges state through small JSON files in one /tmp/sdn-lb-* directory.
This transport is deliberately lab-only.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import tempfile

from .config import BACKENDS

BACKEND_NAMES = frozenset(b.name for b in BACKENDS)


def atomic_json(path: Path, data) -> None:
    """Write via a unique temporary file so concurrent writers never collide."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.stem}-', suffix='.tmp',
                                     dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, UnicodeError, ValueError):
        return default


def read_dict(path: Path) -> dict:
    data = read_json(path, {})
    return data if isinstance(data, dict) else {}


def pid_alive(pid) -> bool:
    """True for a running process; Linux zombies hold no sockets, so False."""
    try:
        pid = int(pid)
        if pid < 2:
            return False
        stat = Path(f'/proc/{pid}/stat').read_text()
        return not stat.rsplit(') ', 1)[1].startswith('Z ')
    except (OSError, ValueError, TypeError, IndexError):
        return False


def read_drains(runtime: Path) -> set[str]:
    names = read_json(runtime / 'drain.json', [])
    return set(names) & BACKEND_NAMES if isinstance(names, list) else set()


def set_drain(runtime: Path, name: str, enabled: bool) -> set[str]:
    """Read-modify-write drain.json under a lock shared by CLI and dashboard."""
    if name not in BACKEND_NAMES:
        raise ValueError(f'unknown backend: {name}')
    with (runtime / 'drain.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        names = read_drains(runtime)
        if enabled:
            names.add(name)
        else:
            names.discard(name)
        atomic_json(runtime / 'drain.json', sorted(names))
    return names
