#!/usr/bin/env bash
# Start Archiveum browser on login
# This script is called by the desktop entry

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../" && pwd)"
PORT="${ARCHIVEUM_PORT:-8000}"

# Wait for the service to start (systemd starts it on boot)
sleep 10

# Determine the URL - try localhost first, then find IP
URL="http://localhost:$PORT/"
if ! curl -s --max-time 5 "$URL" >/dev/null; then
    # Try to find the IP address
    IP=$(hostname -I | awk '{print $1}')
    if [ -n "$IP" ]; then
        URL="http://$IP:$PORT/"
    fi
fi

# Open browser
xdg-open "$URL" || true