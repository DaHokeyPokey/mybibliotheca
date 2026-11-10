"""Let one Gunicorn worker own each background scheduler.

Every worker starts the schedulers, so without this each one would run the
nightly backup and the auto-syncs (six backups a night on the default Docker setup).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows dev server: one process, nothing to share
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
_held: dict[str, int] = {}


def holds_scheduler_lock(name: str, lock_dir: str | os.PathLike) -> bool:
    """Return True if this process owns scheduler `name`.

    Non-blocking. The first worker to ask keeps the lock for its lifetime; the OS
    releases it when that process exits, and the next worker to ask takes over.
    """
    if fcntl is None or name in _held:
        return True
    path = Path(lock_dir) / f".{name}-scheduler.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        # Fail open: duplicate runs beat no backups at all.
        logger.warning("Scheduler lock %s unavailable (%s); running without it", path, exc)
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _held[name] = fd
    return True
