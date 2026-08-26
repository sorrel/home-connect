#!/bin/sh
# Install the recorder as a per-user LaunchAgent.
#
# The tracked plist carries a __REPO__ placeholder rather than a real path, so
# that no home directory ends up in a repository intended to be public. The
# substitution happens here, at install time.
set -eu

repo="$(cd "$(dirname "$0")/.." && pwd)"
label="com.homeconnect.recorder"
target="$HOME/Library/LaunchAgents/$label.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$repo/data"
sed "s|__REPO__|$repo|g" "$repo/launchd/$label.plist" > "$target"

launchctl unload "$target" 2>/dev/null || true
launchctl load "$target"

echo "Loaded $label."
echo "Logs: $repo/data/recorder.log and recorder.err"
echo "Check it is running:  launchctl list | grep homeconnect"
