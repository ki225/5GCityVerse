"""Exclusive cross-platform fence shared by controller/operator processes."""
from contextlib import contextmanager
import os
from pathlib import Path
from backend.onprem.runtime.errors import EvidenceUnavailable

@contextmanager
def controller_fence(path: Path):
    """Same controller byte lock, for a separate operator release process."""
    handle = open(str(path) + ".lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise EvidenceUnavailable("UE controller owns host-loss release ledger") from None
    try:
        yield
    finally:
        handle.close()
