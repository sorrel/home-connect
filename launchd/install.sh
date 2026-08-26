#!/bin/sh
# Install the recorder as a per-user LaunchAgent.
#
# The tracked plist carries __REPO__ and __UV__ placeholders rather than real
# paths, so that no home directory ends up in a repository intended to be
# public. The substitution happens here, at install time.
#
# uv's absolute path is resolved explicitly rather than left as a bare "uv" in
# the plist: launchd runs /bin/sh as a login shell, which reads /etc/profile
# and ~/.profile, not ~/.zshrc — the file that usually puts uv on PATH for an
# interactive zsh session. Without this, the agent would start, fail with
# "uv: command not found", and be silently relaunched every ThrottleInterval
# seconds by KeepAlive, which looks like nothing happening at all.
set -eu

repo="$(cd "$(dirname "$0")/.." && pwd)"
label="com.homeconnect.recorder"
target="$HOME/Library/LaunchAgents/$label.plist"

uv_path="$(command -v uv 2>/dev/null || true)"
if [ -z "$uv_path" ]; then
    echo "error: could not find 'uv' on PATH — the agent was NOT installed." >&2
    echo "Install uv (https://docs.astral.sh/uv/) and re-run this script, or" >&2
    echo "set PATH so 'command -v uv' finds it before installing." >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$repo/data"
sed -e "s|__REPO__|$repo|g" -e "s|__UV__|$uv_path|g" \
    "$repo/launchd/$label.plist" > "$target"

launchctl unload "$target" 2>/dev/null || true
launchctl load "$target"

echo "Loaded $label (using uv at $uv_path)."
echo
echo "To check it actually started:"
echo "  launchctl list | grep homeconnect"
echo "Logs: $repo/data/recorder.log and $repo/data/recorder.err"
