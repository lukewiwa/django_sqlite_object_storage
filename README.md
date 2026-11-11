# Django Sqlite Object Storage

Store a SQLite database in an object storage service (eg. S3, R2, MinIO).

The database is pulled from the bucket just before Django opens it and pushed
back just after Django closes it, but only if it actually changed. Unchanged
databases cost a `stat` and a conditional `GET` (304) per connection.

Supports Python 3.10+ and Django 4.2, 5.2 and 6.0.

## Usage

```python
DATABASES = {
    "default": {
        "ENGINE": "django_sqlite_object_storage",
        "NAME": Path("/tmp", "db.sqlite3"),
        "SQLITE_OBJECT_STORAGE_BUCKET_NAME": "bucketname",
        "SQLITE_OBJECT_STORAGE_ACCESS_KEY_ID": "key",
        "SQLITE_OBJECT_STORAGE_ACCESS_SECRET": "secret",
        "SQLITE_OBJECT_STORAGE_ENDPOINT_URL": "http://localhost:9000",  # Optional
        "SQLITE_OBJECT_STORAGE_REGION_NAME": "us-east-1",  # Optional
        "SQLITE_OBJECT_STORAGE_SIGNATURE_VERSION": "s3v4",  # Optional, default s3v4
    }
}
```

The object key is the file name of `NAME`.

## Caveats

- This is a single writer setup. Two processes writing at once will clobber
  each other, last close wins.
- In-memory databases (including Django's default SQLite test database) never
  touch object storage.

## Development

```bash
make test
```

Tests run against a mocked S3 via [moto](https://github.com/getmoto/moto), no
bucket required.
