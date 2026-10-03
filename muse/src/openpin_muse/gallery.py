"""Private, quota-bounded originals stored on the Linux companion."""

import asyncio
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat

from .protocol import validate_capture


CAPTURE_SUFFIXES = {"image/jpeg": ".jpg", "video/mp4": ".mp4"}


class GalleryFull(Exception):
    """Saving this capture would exceed the configured disk quota."""


class Gallery:
    def __init__(self, state_dir: Path, max_bytes: int = 5 * 1024**3):
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("Gallery quota must be a positive byte count")
        self.max_bytes = max_bytes
        self.directory = Path(state_dir) / "gallery"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ValueError("Gallery must be a real directory")
        self.directory.chmod(0o700)
        self.database = self.directory / "index.sqlite3"
        fd = os.open(self.database, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Gallery index must be a regular file")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self._locked() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS captures (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, mime_type TEXT NOT NULL,
                size INTEGER NOT NULL, filename TEXT NOT NULL, muse_status TEXT NOT NULL
            )""")

    @contextmanager
    def _locked(self):
        fd = os.open(self.directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            if self.database.is_symlink():
                raise ValueError("Gallery index cannot be a symlink")
            connection = sqlite3.connect(self.database)
            connection.row_factory = sqlite3.Row
            try:
                with connection:
                    yield connection
            finally:
                connection.close()
        finally:
            os.close(fd)

    @staticmethod
    def _valid_id(capture_id):
        return isinstance(capture_id, str) and re.fullmatch(r"[0-9a-f]{32}", capture_id) is not None

    def _save(self, data, mime_type):
        validate_capture(data, mime_type)
        suffix = CAPTURE_SUFFIXES[mime_type]
        with self._locked() as connection:
            # Include unindexed originals left by an interrupted save, so crashes
            # cannot silently bypass the quota. Existing media is never evicted.
            used = sum(path.lstat().st_size for path in self.directory.iterdir()
                       if re.fullmatch(r"[0-9a-f]{32}\.(jpg|mp4)", path.name))
            if used + len(data) > self.max_bytes:
                raise GalleryFull("Gallery is full. Download or delete captures before trying again.")
            capture_id = secrets.token_hex(16)
            metadata = {"id": capture_id, "created_at": datetime.now(timezone.utc).isoformat(),
                        "mime_type": mime_type, "size": len(data),
                        "filename": capture_id + suffix, "muse_status": "pending"}
            path = self.directory / metadata["filename"]
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                connection.execute("INSERT INTO captures VALUES (:id, :created_at, :mime_type, :size, "
                                   ":filename, :muse_status)", metadata)
                connection.commit()
            except BaseException:
                with suppress(FileNotFoundError):
                    path.unlink()
                raise
            return metadata

    async def save(self, data: bytes, mime_type: str) -> dict:
        return await asyncio.to_thread(self._save, data, mime_type)

    def _list(self):
        with self._locked() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM captures ORDER BY created_at DESC")]

    async def list(self) -> list[dict]:
        return await asyncio.to_thread(self._list)

    def _get(self, capture_id):
        if not self._valid_id(capture_id):
            return None
        with self._locked() as connection:
            row = connection.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
            if row is None:
                return None
            metadata = dict(row)
            expected = capture_id + CAPTURE_SUFFIXES[metadata["mime_type"]]
            if metadata["filename"] != expected:
                return None
            path = self.directory / expected
            try:
                info = path.lstat()
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                return None
            return metadata, path

    async def get(self, capture_id: str) -> tuple[dict, Path] | None:
        return await asyncio.to_thread(self._get, capture_id)

    def _delete(self, capture_id):
        if not self._valid_id(capture_id):
            return False
        with self._locked() as connection:
            row = connection.execute("SELECT mime_type FROM captures WHERE id = ?", (capture_id,)).fetchone()
            if row is None:
                return False
            suffix = CAPTURE_SUFFIXES[row["mime_type"]]
            with suppress(FileNotFoundError):
                (self.directory / (capture_id + suffix)).unlink()
            connection.execute("DELETE FROM captures WHERE id = ?", (capture_id,))
            return True

    async def delete(self, capture_id: str) -> bool:
        return await asyncio.to_thread(self._delete, capture_id)

    def _mark_forwarded(self, capture_id, status):
        if status not in ("pending", "sent", "failed") or not self._valid_id(capture_id):
            raise ValueError("Invalid forwarding status or capture ID")
        with self._locked() as connection:
            connection.execute("UPDATE captures SET muse_status = ? WHERE id = ?", (status, capture_id))

    async def mark_forwarded(self, capture_id: str, status: str) -> None:
        await asyncio.to_thread(self._mark_forwarded, capture_id, status)
