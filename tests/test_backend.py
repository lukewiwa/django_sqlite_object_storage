import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import boto3
from django.core.signals import request_finished
from django.db.utils import ConnectionHandler
from moto import mock_aws

from django_sqlite_object_storage.base import ObjectStorageConflict

BUCKET = "test-bucket"


class ObjectStorageBackendTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(
            os.environ,
            {
                "AWS_ACCESS_KEY_ID": "testing",
                "AWS_SECRET_ACCESS_KEY": "testing",
                "AWS_DEFAULT_REGION": "us-east-1",
            },
        )
        env.start()
        self.addCleanup(env.stop)

        aws = mock_aws()
        aws.start()
        self.addCleanup(aws.stop)

        self.s3 = boto3.client("s3", region_name="us-east-1")
        self.s3.create_bucket(Bucket=BUCKET)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def make_connection(self, directory="a", name="db.sqlite3"):
        path = self.tmp / directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = ConnectionHandler(
            {
                "default": {
                    "ENGINE": "django_sqlite_object_storage",
                    "NAME": path,
                    "SQLITE_OBJECT_STORAGE_BUCKET_NAME": BUCKET,
                    "SQLITE_OBJECT_STORAGE_ACCESS_KEY_ID": "testing",
                    "SQLITE_OBJECT_STORAGE_ACCESS_SECRET": "testing",
                    "SQLITE_OBJECT_STORAGE_REGION_NAME": "us-east-1",
                }
            }
        )
        conn = handler["default"]
        self.addCleanup(conn.close)
        return conn

    def spy(self, conn, method):
        client = conn.object_storage_client
        patcher = mock.patch.object(client, method, wraps=getattr(client, method))
        self.addCleanup(patcher.stop)
        return patcher.start()

    def remote_body(self, key="db.sqlite3"):
        return self.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()

    def write(self, conn, value):
        with conn.cursor() as cursor:
            cursor.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
            cursor.execute("INSERT INTO t VALUES (%s)", [value])

    def read(self, conn):
        with conn.cursor() as cursor:
            cursor.execute("SELECT v FROM t ORDER BY v")
            return [row[0] for row in cursor.fetchall()]

    def test_new_database_is_uploaded_on_close(self):
        conn = self.make_connection()
        self.write(conn, "hello")
        conn.close()
        self.assertEqual(self.remote_body(), conn.database_local_path.read_bytes())

    def test_round_trip_between_hosts(self):
        a = self.make_connection("a")
        self.write(a, "from a")
        a.close()

        b = self.make_connection("b")
        self.assertEqual(self.read(b), ["from a"])
        self.write(b, "from b")
        b.close()

        a.connect()
        self.assertEqual(self.read(a), ["from a", "from b"])

    def test_unchanged_database_is_not_uploaded(self):
        conn = self.make_connection()
        self.write(conn, "hello")
        conn.close()

        put = self.spy(conn, "put_object")
        self.read(conn)
        conn.close()
        put.assert_not_called()

    def test_unchanged_database_is_not_downloaded(self):
        conn = self.make_connection()
        self.write(conn, "hello")
        conn.close()

        with mock.patch("django_sqlite_object_storage.base.os.replace") as replace:
            conn.connect()
        replace.assert_not_called()

    def test_close_without_connect_does_not_upload(self):
        conn = self.make_connection()
        conn.database_local_path.write_bytes(b"stale junk")
        put = self.spy(conn, "put_object")
        conn.close()
        put.assert_not_called()

    def test_remote_change_replaces_local_and_drops_stale_journal(self):
        a = self.make_connection("a")
        self.write(a, "one")
        a.close()

        b = self.make_connection("b")
        self.write(b, "two")
        b.close()

        journal = Path(f"{a.database_local_path}-journal")
        journal.write_bytes(b"left over from a crash")
        a.connect()
        self.assertFalse(journal.exists())
        self.assertEqual(self.read(a), ["one", "two"])

    def test_failed_download_leaves_local_file_intact(self):
        a = self.make_connection("a")
        self.write(a, "one")
        a.close()
        b = self.make_connection("b")
        self.write(b, "two")
        b.close()

        before = a.database_local_path.read_bytes()
        with (
            mock.patch(
                "django_sqlite_object_storage.base._md5_stream",
                side_effect=OSError("network went away"),
            ),
            self.assertRaises(OSError),
        ):
            a.connect()
        self.assertEqual(a.database_local_path.read_bytes(), before)
        leftovers = [
            p.name
            for p in a.database_local_path.parent.iterdir()
            if p.name.startswith(".")
        ]
        self.assertEqual(leftovers, [])

    def test_hash_is_cached_for_untouched_file(self):
        conn = self.make_connection()
        self.write(conn, "hello")
        conn.close()
        # Backdate the file so it's outside the racy window.
        os.utime(conn.database_local_path, ns=(0, 0))
        first = conn.local_md5()
        with mock.patch("django_sqlite_object_storage.base._md5_stream") as md5:
            self.assertEqual(conn.local_md5(), first)
        md5.assert_not_called()

    def test_in_memory_database_never_touches_storage(self):
        conn = self.make_connection()
        conn.settings_dict["NAME"] = ":memory:"
        get = self.spy(conn, "get_object")
        self.write(conn, "hello")
        get.assert_not_called()

    def test_concurrent_writers_conflict_instead_of_clobbering(self):
        a = self.make_connection("a")
        b = self.make_connection("b")
        self.write(a, "seed")
        a.close()

        a.connect()
        b.connect()
        self.write(a, "from a")
        self.write(b, "from b")
        a.close()
        with self.assertRaises(ObjectStorageConflict):
            b.close()

        # b's write was refused; reconnecting picks up a's version.
        b.connect()
        self.assertEqual(self.read(b), ["from a", "seed"])

    def test_concurrent_creation_conflicts(self):
        a = self.make_connection("a")
        b = self.make_connection("b")
        a.connect()
        b.connect()
        self.write(a, "from a")
        self.write(b, "from b")
        a.close()
        with self.assertRaises(ObjectStorageConflict):
            b.close()

    def test_fresh_process_uses_stored_etag(self):
        conn = self.make_connection()
        self.write(conn, "hello")
        conn.close()
        etag = self.s3.head_object(Bucket=BUCKET, Key="db.sqlite3")["ETag"]

        fresh = self.make_connection()
        get = self.spy(fresh, "get_object")
        fresh.connect()
        self.assertEqual(get.call_args.kwargs["IfNoneMatch"], etag)
        self.assertEqual(self.read(fresh), ["hello"])

    def test_unpushed_changes_survive_reconnect_if_remote_unchanged(self):
        conn = self.make_connection()
        self.write(conn, "one")
        conn.close()

        self.write(conn, "two")
        with (
            mock.patch.object(
                conn.object_storage_client, "put_object", side_effect=OSError
            ),
            self.assertRaises(OSError),
        ):
            conn.close()

        fresh = self.make_connection()
        self.assertEqual(self.read(fresh), ["one", "two"])
        fresh.close()
        self.assertEqual(self.remote_body(), fresh.database_local_path.read_bytes())

    def remote_rows(self):
        path = self.tmp / "remote.sqlite3"
        path.write_bytes(self.remote_body())
        with sqlite3.connect(path) as db:
            return [r[0] for r in db.execute("SELECT v FROM t ORDER BY v")]

    def test_sync_uploads_without_closing(self):
        conn = self.make_connection()
        self.write(conn, "hello")
        self.assertTrue(conn.sync())
        self.assertIsNotNone(conn.connection)
        self.assertEqual(self.remote_rows(), ["hello"])

        # Still usable, and close doesn't upload again.
        self.write(conn, "again")
        self.assertTrue(conn.sync())
        put = self.spy(conn, "put_object")
        conn.close()
        put.assert_not_called()
        self.assertEqual(self.remote_rows(), ["again", "hello"])

    def test_sync_noop_when_unchanged(self):
        conn = self.make_connection()
        self.write(conn, "hello")
        conn.sync()
        put = self.spy(conn, "put_object")
        self.read(conn)
        self.assertFalse(conn.sync())
        put.assert_not_called()

    def test_sync_skipped_inside_transaction(self):
        conn = self.make_connection()
        self.write(conn, "committed")
        conn.sync()

        conn.set_autocommit(False)
        self.write(conn, "uncommitted")
        self.assertFalse(conn.sync())
        conn.rollback()
        conn.set_autocommit(True)
        self.assertEqual(self.remote_rows(), ["committed"])

    def test_sync_in_wal_mode(self):
        conn = self.make_connection()
        with conn.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=wal")
        self.write(conn, "hello")
        self.assertTrue(conn.sync())
        self.assertEqual(self.remote_rows(), ["hello"])
        self.assertFalse(conn.sync())

    def test_request_finished_syncs_open_connections(self):
        conn = self.make_connection()
        self.write(conn, "hello")
        with mock.patch(
            "django_sqlite_object_storage.base.connections.all",
            return_value=[conn],
        ):
            request_finished.send(sender=None)
        self.assertEqual(self.remote_rows(), ["hello"])
