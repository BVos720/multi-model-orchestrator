from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

# One real risk in every file-writing mode: two agents (or two separate
# `orchest` invocations) editing the same folder at once, silently
# clobbering or merge-conflicting each other's work. We can't lock at the
# individual-file level for the cloud CLIs (Claude Code/Codex/Copilot
# manage their own internal edits - we never see which specific files
# they touch), so the only granularity that's actually enforceable is
# the whole folder: one agent works in a given folder at a time, full stop.
_LOCK_FILE_NAME = ".orchest-folder.lock"

# Guards concurrent calls WITHIN this one process (e.g. two coroutines in
# the same run) - the lock file alone only stops separate PROCESSES,
# since two asyncio tasks in one process could otherwise both "win" the
# same os.O_EXCL race in the gap between check and use on some platforms.
_in_process_locks: dict[str, asyncio.Lock] = {}


def _lock_path(folder: str) -> Path:
    return Path(folder).resolve() / _LOCK_FILE_NAME


def _claim_lock_file(folder: str, owner: str, timeout: float, stale_after: float) -> Path:
    """The actual cross-process claim, shared by both the async and sync
    context managers below. Raises RuntimeError on timeout - see
    folder_lock's docstring for what that means."""
    lock_file = _lock_path(folder)
    deadline = time.time() + timeout
    while True:
        try:
            if lock_file.exists() and time.time() - lock_file.stat().st_mtime > stale_after:
                lock_file.unlink()  # stale - reclaim rather than block forever
            lock_file.parent.mkdir(parents=True, exist_ok=True)
            # O_EXCL: fails if the file already exists - the atomic
            # "claim it" primitive this whole thing depends on.
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, owner.encode("utf-8"))
            os.close(fd)
            return lock_file
        except FileExistsError:
            if time.time() >= deadline:
                try:
                    holder = lock_file.read_text(encoding="utf-8").strip()
                except OSError:
                    holder = "another agent"
                raise RuntimeError(
                    f"{folder} is already locked (by {holder}) - refusing to risk two agents "
                    f"editing the same files at once. Wait for it to finish, or delete "
                    f"{lock_file} yourself if you're sure nothing's actually running there."
                )
            time.sleep(0.2)


@contextlib.asynccontextmanager
async def folder_lock(folder: str, owner: str, timeout: float = 5.0, stale_after: float = 3600.0):
    """Hold exclusive rights to work in `folder` for the `async with` block.
    `owner` is just a label (agent name) written into the lock file so a
    human can see who's holding it.

    Raises RuntimeError if the folder is already locked by someone else
    and doesn't free up within `timeout`s - callers should treat that as
    "someone else is already working here," not barge in. A lock file
    older than `stale_after` is treated as abandoned (e.g. a crashed
    process that never cleaned up) and reclaimed rather than blocking
    forever on a lock nobody's actually holding anymore.

    See `sync_folder_lock` for the plain (non-async) equivalent, used by
    the one caller (run_interactive) that blocks the whole process on a
    foreground subprocess rather than awaiting anything.
    """
    resolved = str(Path(folder).resolve())
    in_process = _in_process_locks.setdefault(resolved, asyncio.Lock())
    try:
        await asyncio.wait_for(in_process.acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"{folder} is already locked by another task in this process - refusing to risk two "
            f"agents editing the same files at once. Wait for it to finish before trying again."
        )
    try:
        lock_file = await asyncio.to_thread(_claim_lock_file, folder, owner, timeout, stale_after)
        try:
            yield
        finally:
            try:
                lock_file.unlink()
            except OSError:
                pass
    finally:
        in_process.release()


@contextlib.contextmanager
def sync_folder_lock(folder: str, owner: str, timeout: float = 5.0, stale_after: float = 3600.0):
    """Plain (non-async) equivalent of `folder_lock`, for a caller that's
    just going to block the whole process anyway (a foreground
    subprocess.run) rather than await anything - no in-process
    asyncio.Lock needed here since nothing else in this process runs
    concurrently while such a call blocks. Same cross-process lock file
    underneath, so it still guards against a second `orchest` process."""
    lock_file = _claim_lock_file(folder, owner, timeout, stale_after)
    try:
        yield
    finally:
        try:
            lock_file.unlink()
        except OSError:
            pass
