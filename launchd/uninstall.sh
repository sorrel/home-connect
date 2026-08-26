#!/bin/sh
# Remove the recorder LaunchAgent. Leaves data/ alone.
set -eu

label="com.homeconnect.recorder"
target="$HOME/Library/LaunchAgents/$label.plist"

launchctl unload "$target" 2>/dev/null || true
rm -f "$target"

echo "Removed $label."
echo "The event log in data/ has been left untouched — it cannot be rebuilt"
echo "from the API, so it is never deleted by this script."
