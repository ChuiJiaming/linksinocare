#!/bin/sh
# Render start command: ./start.sh
# server.py must sit alongside the website files, so copy them together first.
set -e
cp backend/server.py ./server.py
cp -r public/. .
exec python server.py
