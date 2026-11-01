#!/bin/sh
# Starts the mock Drex API, then Postgres as the image's own entrypoint does
set -e
python3 /mock/drex_mock.py --host 0.0.0.0 --port 8787 &
exec docker-entrypoint.sh "$@"
