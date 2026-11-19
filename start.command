#!/bin/sh
# Double-click in Finder to start Quiet Wire and open it in your browser.
cd "$(dirname "$0")" || exit 1
(sleep 2; open "http://localhost:8791") &
exec /usr/bin/python3 server.py --port 8791 "$@"
