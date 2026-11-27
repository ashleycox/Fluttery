"""Recover interrupted output writes without scanning the user's files."""

from __future__ import annotations

import json
import os
import re
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from platformdirs import user_cache_path

_JOURNAL_NAME = re.compile(r"[0-9a-f]{32}\.json")
_PART_NAME = ".fluttery-stage-{}.part"


def _staging_directory():
    return user_cache_path("Fluttery", appauthor=False) / "pending-writes"


def _lock(stream, *, wait):
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        # Hold the byte range until the journal's handle is closed. Windows
        # keeps the lock across seek operations while the descriptor is read.
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))


def cleanup_session_staging():
    """Remove only temporary files registered by a writer that has exited."""
    for journal in _staging_directory().glob("*.json"):
        if not _JOURNAL_NAME.fullmatch(journal.name) or journal.is_symlink():
            continue
        try:
            with journal.open("r+b") as descriptor:
                _lock(descriptor, wait=False)
                try:
                    stage = Path(json.loads(descriptor.read(65_536))["stage"])
                except (KeyError, TypeError, ValueError):
                    # The journal is created before its writer takes the
                    # lock. Give a new, as yet empty descriptor time to arm.
                    if time.time() - journal.stat().st_mtime < 86_400:
                        continue
                else:
                    if stage.is_absolute() and stage.name == _PART_NAME.format(journal.stem):
                        stage.unlink(missing_ok=True)
            journal.unlink(missing_ok=True)
        except OSError:
            continue  # Active writer, vanished journal, or a disconnected destination volume.


@contextmanager
def atomic_output(destination, *, mode="w+b"):
    """Yield an owned file on the destination volume, then replace in one move."""
    destination = Path(os.path.abspath(destination))
    identifier = uuid4().hex
    registry = _staging_directory()
    registry.mkdir(parents=True, exist_ok=True)
    journal = registry / f"{identifier}.json"
    stage = destination.with_name(_PART_NAME.format(identifier))
    owns_stage = False
    try:
        with journal.open("x+b") as descriptor:
            _lock(descriptor, wait=True)
            descriptor.write(json.dumps({"stage": str(stage)}).encode("utf-8"))
            descriptor.flush()
            os.fsync(descriptor.fileno())
            try:
                options = {"encoding": "utf-8", "newline": ""} if "b" not in mode else {}
                with stage.open(mode.replace("w", "x"), **options) as stream:
                    owns_stage = True
                    if os.name == "posix":
                        # Replacing a private existing report or session must
                        # not broaden its access through the process umask.
                        try:
                            previous = destination.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            if stat.S_ISREG(previous.st_mode):
                                os.fchmod(stream.fileno(), previous.st_mode & 0o777)
                    yield stream
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(stage, destination)
                owns_stage = False
            finally:
                if owns_stage:
                    try:
                        stage.unlink(missing_ok=True)
                    except OSError:
                        pass  # Preserve the journal for recovery after the volume returns.
                    else:
                        owns_stage = False
    finally:
        if not owns_stage:
            try:
                journal.unlink(missing_ok=True)
            except OSError:
                pass  # A later startup can clear a locked or disconnected journal.
