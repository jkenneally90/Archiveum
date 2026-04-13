#!/bin/bash
# Stop Archiveum
# This script stops the Archiveum systemd service or kills running processes

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[Archiveum] Stopping Archiveum..."

# Try systemd first
if systemctl is-active --quiet archiveum.service 2>/dev/null; then
    echo "[Archiveum] Stopping systemd service..."
    sudo systemctl stop archiveum.service
    echo "[Archiveum] Service stopped."
    exit 0
fi

# Fallback: kill any running main.py processes
PIDS=$(pgrep -f "python.*main.py" || true)
if [ -n "$PIDS" ]; then
    echo "[Archiveum] Stopping running processes..."
    kill $PIDS 2>/dev/null || true
    sleep 2
    # Force kill if still running
    PIDS=$(pgrep -f "python.*main.py" || true)
    if [ -n "$PIDS" ]; then
        kill -9 $PIDS 2>/dev/null || true
    fi
    echo "[Archiveum] Stopped."
else
    echo "[Archiveum] No running process found."
fi
