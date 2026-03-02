#!/bin/bash
# Reliably restart Nico Jr. — kills the running instance using the PID file
# Usage: ./restart.sh

PID_FILE="$(dirname "$0")/.alfred.pid"

if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Stopping Nico Jr. (PID $OLD_PID)..."
    kill "$OLD_PID"
    sleep 1
  fi
  rm -f "$PID_FILE"
fi

echo "Starting Nico Jr...."
source "$(dirname "$0")/.venv/bin/activate"
python3 "$(dirname "$0")/main.py"
