#!/bin/bash

poetry install
# poetry run ./manage.py create_test_bucket

exec "$@"
