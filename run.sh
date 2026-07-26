#!/usr/bin/env bash
# Start Jake the familiar.
# gtk4-layer-shell MUST be loaded before libwayland: hence LD_PRELOAD.
cd "$(dirname "$0")" || exit 1
SO=/usr/lib/libgtk4-layer-shell.so
if [ -e "$SO" ]; then
    export LD_PRELOAD="${LD_PRELOAD:+$LD_PRELOAD:}$SO"
fi
exec python3 -m jake
