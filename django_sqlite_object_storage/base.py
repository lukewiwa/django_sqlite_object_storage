from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import tempfile
import time
from functools import cached_property
from pathlib import Path
from typing import BinaryIO

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.core.signals import request_finished
from django.db import connections
from django.db.backends.sqlite3 import base
from django.db.utils import DatabaseError

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024

# A cached hash is only trusted if the file was last modified at least this long
# before the hash was taken. Filesystem timestamps can be coarse, so a write
# landing in the same tick as the hash (with the same size) would otherwise be
# missed. Same trick git uses for "racily clean" index entries.
RACY_WINDOW_NS = 2_000_000_000

# Files SQLite keeps alongside the database. If the main file is replaced with a
# fresh copy from object storage these belong to the old file and must go, or
# SQLite will happily replay them over the new one.
SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


STATE_SUFFIX = ".s3state"

SYNC_ATTEMPTS = 3

CONFLICT_ERROR_CODES = ("PreconditionFailed", "ConditionalRequestConflict")


class ObjectStorageConflict(DatabaseError):
    """
    The remote database changed since we last synced, so our upload was
    refused rather than overwriting someone else's writes.
    """


def _md5_stream(stream: BinaryIO, sink: BinaryIO | None = None) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    while chunk := stream.read(CHUNK_SIZE):
        digest.update(chunk)
        if sink is not None:
            sink.write(chunk)
    return digest.hexdigest()


def _stat_key(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class DatabaseWrapper(base.DatabaseWrapper):
    """
    Wraps the normal Django SQLite DB engine in one that shuttles the SQLite
    database back and forth from an S3 compatible bucket.

    The database is pulled (if it changed remotely) just before SQLite opens the
    file, and pushed (if it changed locally) just after SQLite closes it.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # md5 of the local file at the point we last knew it matched the remote.
        self._synced_md5: str | None = None
        # ETag of the remote object at that point, None if it didn't exist.
        self._remote_etag: str | None = None
        # (stat key, time hashed, md5) so we don't rehash an unchanged file.
        self._md5_cache: tuple[tuple[int, int, int, int], int, str] | None = None

    @property
    def database_local_path(self) -> Path:
        return Path(self.settings_dict["NAME"])

    @property
    def database_remote_path(self) -> str:
        return self.database_local_path.name

    @property
    def state_path(self) -> Path:
        path = self.database_local_path
        return path.with_name(f"{path.name}{STATE_SUFFIX}")

    @cached_property
    def bucket_name(self) -> str:
        return self.settings_dict["SQLITE_OBJECT_STORAGE_BUCKET_NAME"]

    @property
    def object_storage_enabled(self) -> bool:
        return not self.is_in_memory_db()  # pyright: ignore[reportAttributeAccessIssue]

    @cached_property
    def object_storage_client(self):
        settings = self.settings_dict
        session = boto3.Session(
            aws_access_key_id=settings.get("SQLITE_OBJECT_STORAGE_ACCESS_KEY_ID"),
            aws_secret_access_key=settings.get("SQLITE_OBJECT_STORAGE_ACCESS_SECRET"),
            region_name=settings.get("SQLITE_OBJECT_STORAGE_REGION_NAME"),
        )
        return session.client(
            "s3",
            config=Config(
                signature_version=settings.get(
                    "SQLITE_OBJECT_STORAGE_SIGNATURE_VERSION", "s3v4"
                )
            ),
            endpoint_url=settings.get("SQLITE_OBJECT_STORAGE_ENDPOINT_URL"),
        )

    def get_object_storage_client(self):
        # Kept for backwards compatibility.
        return self.object_storage_client

    def _remember_md5(self, md5: str, hashed_at_ns: int) -> None:
        st = self.database_local_path.stat()
        self._md5_cache = (_stat_key(st), hashed_at_ns, md5)

    def local_md5(self) -> str | None:
        """md5 of the local database file, or None if it doesn't exist."""
        path = self.database_local_path
        try:
            st = path.stat()
        except FileNotFoundError:
            return None
        if self._md5_cache is not None:
            key, hashed_at_ns, md5 = self._md5_cache
            if key == _stat_key(st) and hashed_at_ns - st.st_mtime_ns > RACY_WINDOW_NS:
                return md5
        hashed_at_ns = time.time_ns()
        with path.open("rb") as f:
            md5 = _md5_stream(f)
        self._remember_md5(md5, hashed_at_ns)
        return md5

    # Kept for backwards compatibility.
    @property
    def local_db_md5(self) -> str | None:
        return self.local_md5()

    def get_new_connection(self, conn_params):
        if self.object_storage_enabled:
            self.pull_from_object_storage()
        return super().get_new_connection(conn_params)

    def _close(self):
        super()._close()  # pyright: ignore[reportAttributeAccessIssue]
        if self.object_storage_enabled:
            self.push_to_object_storage()

    def _load_state(self) -> dict | None:
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, ValueError):
            return None

    def _save_state(self) -> None:
        """
        Persist which remote version the local file corresponds to, so a fresh
        process can do a conditional GET on the real ETag and knows what to
        expect when it uploads.
        """
        state = {"etag": self._remote_etag, "md5": self._synced_md5}
        path = self.state_path
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, path)

    def pull_from_object_storage(self) -> None:
        """
        Replace the local database with the remote one if the remote changed
        since we last synced.
        """
        path = self.database_local_path
        state = self._load_state()
        local_md5 = self.local_md5()
        kwargs = {"Bucket": self.bucket_name, "Key": self.database_remote_path}
        if local_md5 is not None:
            if state and state.get("etag"):
                kwargs["IfNoneMatch"] = state["etag"]
            else:
                # No record of the remote ETag, fall back to guessing. For a
                # single part, non-KMS upload it's the quoted md5 of the body.
                kwargs["IfNoneMatch"] = f'"{local_md5}"'

        try:
            response = self.object_storage_client.get_object(**kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("304", "NotModified"):
                # Remote hasn't moved since we last synced. Keep the local file
                # even if it has unpushed changes, they'll go up on close.
                self._remote_etag = kwargs["IfNoneMatch"]
                if state and state.get("etag") == self._remote_etag:
                    self._synced_md5 = state.get("md5")
                else:
                    self._synced_md5 = local_md5
                    self._save_state()
                logger.info("[DB Connect] %s unchanged remotely", path)
                return
            if code == "NoSuchKey":
                logger.warning(
                    "[DB Connect] %s doesn't exist in object storage yet",
                    self.database_remote_path,
                )
                # Remote is empty: whatever is local needs pushing on close.
                self._remote_etag = None
                self._synced_md5 = None
                self._save_state()
                return
            raise

        if local_md5 is not None and state and local_md5 != state.get("md5"):
            logger.warning(
                "[DB Connect] %s has local changes that were never uploaded and the"
                " remote has since changed. Discarding the local changes.",
                path,
            )

        # Stream into a temp file next to the target, then atomically swap it in
        # so a failed download never leaves a truncated database behind.
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            hashed_at_ns = time.time_ns()
            with os.fdopen(fd, "wb") as tmp, response["Body"] as body:
                md5 = _md5_stream(body, sink=tmp)
            for suffix in SIDECAR_SUFFIXES:
                Path(f"{path}{suffix}").unlink(missing_ok=True)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

        self._remember_md5(md5, hashed_at_ns)
        self._synced_md5 = md5
        self._remote_etag = response["ETag"]
        self._save_state()
        logger.info("[DB Connect] Downloaded %s (md5:%s)", path, md5)

    def push_to_object_storage(self) -> None:
        """
        Upload the local database if it changed since the last sync, but only if
        the remote is still the version we started from.
        """
        path = self.database_local_path
        local_md5 = self.local_md5()
        if local_md5 is None:
            return
        if local_md5 == self._synced_md5:
            logger.debug(
                "[DB Close] %s unchanged (md5:%s), not uploading", path, local_md5
            )
            return

        logger.debug("[DB Close] %s changed (md5:%s), uploading", path, local_md5)
        self._upload(path, local_md5)

    def sync(self) -> bool:
        """
        Upload committed changes without closing the connection. Returns True if
        anything was uploaded.

        Briefly takes SQLite's write lock (readers aren't blocked) while copying
        the file, so the upload is a consistent snapshot. Does nothing inside a
        transaction, or if the connection isn't open.
        """
        conn = self.connection
        if (
            not self.object_storage_enabled
            or conn is None
            or self.in_atomic_block
            or conn.in_transaction
        ):
            return False

        path = self.database_local_path
        wal_path = Path(f"{path}-wal")
        wal = conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

        def wal_is_empty() -> bool:
            try:
                return wal_path.stat().st_size == 0
            except FileNotFoundError:
                return True

        for _ in range(SYNC_ATTEMPTS):
            if wal:
                # Can't checkpoint inside a transaction, so do it first and check
                # nothing snuck into the WAL once we hold the lock.
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if self.local_md5() == self._synced_md5 and (not wal or wal_is_empty()):
                return False

            snapshot = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                if not wal or wal_is_empty():
                    snapshot = self._snapshot(path)
            finally:
                conn.execute("ROLLBACK")
            if snapshot is not None:
                break
        else:
            logger.warning(
                "[DB Sync] %s kept changing, giving up until the next sync", path
            )
            return False

        snapshot_path, md5 = snapshot
        try:
            if md5 == self._synced_md5:
                return False
            self._upload(snapshot_path, md5)
        finally:
            snapshot_path.unlink(missing_ok=True)
        return True

    def _snapshot(self, path: Path) -> tuple[Path, str]:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "wb") as tmp, path.open("rb") as src:
                md5 = _md5_stream(src, sink=tmp)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return Path(tmp_name), md5

    def _upload(self, source: Path, md5: str) -> None:
        """
        Upload source as the remote database, but only if the remote is still the
        version we last synced.
        """
        kwargs = {
            "Bucket": self.bucket_name,
            "Key": self.database_remote_path,
            "ContentMD5": base64.b64encode(bytes.fromhex(md5)).decode(),
        }
        if self._remote_etag is None:
            kwargs["IfNoneMatch"] = "*"
        else:
            kwargs["IfMatch"] = self._remote_etag

        try:
            with source.open("rb") as f:
                response = self.object_storage_client.put_object(Body=f, **kwargs)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in CONFLICT_ERROR_CODES:
                raise ObjectStorageConflict(
                    f"{self.database_remote_path} was changed by someone else since"
                    f" it was last synced (expected ETag {self._remote_etag})."
                    " Local changes were not uploaded."
                ) from e
            raise

        self._synced_md5 = md5
        self._remote_etag = response["ETag"]
        self._save_state()
        logger.info("[DB Sync] Uploaded %s (md5:%s)", self.database_local_path, md5)


def sync_open_connections(**kwargs) -> None:
    """
    Push changes at the end of each request for connections Django keeps open
    (CONN_MAX_AGE > 0). Connections Django closes are pushed on close anyway.
    """
    for conn in connections.all(initialized_only=True):  # pyright: ignore[reportCallIssue]
        if isinstance(conn, DatabaseWrapper):
            conn.sync()


# Connected after django.db's close_old_connections, so connections due to be
# closed are already closed (and pushed) by the time this runs.
request_finished.connect(sync_open_connections)
