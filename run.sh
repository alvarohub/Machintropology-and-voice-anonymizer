#!/usr/bin/env bash
# Launch the speech-to-emotion pipeline
# Usage: ./run.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Activate conda env if available
if command -v conda &>/dev/null; then
    eval "$(conda shell.bash hook)"
    conda activate ML311
fi

python strip_monitor.py "$@"
