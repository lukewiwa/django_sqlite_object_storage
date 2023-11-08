# Django Sqlite Object Storage

Store sqlite database in an object storage service (eg. S3)

## Usage

Install the app in `settings.py`

```python
DATABASES = {
    "default": {
        "ENGINE": "django_sqlite_object_storage",
        "NAME": Path("/tmp", "db.sqlite3"),
        "SQLITE_OBJECT_STORAGE_BUCKET_NAME": "bucektname",
        "SQLITE_OBJECT_STORAGE_ACCESS_KEY_ID": "key",
        "SQLITE_OBJECT_STORAGE_ACCESS_SECRET": "secret",
    }
}
```
