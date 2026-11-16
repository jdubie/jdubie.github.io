#!/bin/sh
mkdir -p "${DATA_DIR:-/tmp/data}" 2>/dev/null
exec python3 -u /work/server.py
