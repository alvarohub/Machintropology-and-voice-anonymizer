#!/usr/bin/env bash
# heartbeat.sh — external "tick" for the Chronicler.
#
# VS Code Copilot has no built-in scheduler; this is the prosthesis.
# Every INTERVAL seconds, fire a macOS notification reminding the
# biological agent to ping the worker with a chronicling prompt.
#
# Usage:
#   ./chronicle/heartbeat.sh                 # default 30 min
#   INTERVAL=900 ./chronicle/heartbeat.sh    # 15 min
#
# Stop with Ctrl-C.

INTERVAL="${INTERVAL:-1800}"   # seconds; default 30 min
LABEL="${LABEL:-Pi porting day}"
PROMPT='Heartbeat tick — invoke @Chronicle and @Ethnographer. Each must read the latest transcript directly before deciding to write or remain silent.'

# Where the live VS Code Copilot Chat transcripts for this workspace live.
# (Workspace hash is stable; the .jsonl filename is per-conversation — the
# observers should read the most recently modified file in this dir.)
TRANSCRIPT_DIR="$HOME/Library/Application Support/Code/User/workspaceStorage/664202b45e1337c7e75f3dc145af92b3/GitHub.copilot-chat/transcripts"

echo "[heartbeat] every ${INTERVAL}s — ${LABEL}"
echo "[heartbeat] suggested prompt: ${PROMPT}"
echo "[heartbeat] transcript dir: ${TRANSCRIPT_DIR}"
echo
echo "[heartbeat] When a tick fires, in chat type either:"
echo "  @Chronicle    read the latest .jsonl in the transcript dir (see CHRONICLER.md §On each invocation), then decide."
echo "  @Ethnographer same — read the transcript directly. Do not rely on the worker's framing."
echo

while true; do
  TS="$(date +%H:%M)"
  osascript -e "display notification \"${PROMPT}\" with title \"Chronicler tick — ${LABEL}\" subtitle \"${TS}\" sound name \"Tink\""
  echo "[heartbeat] ${TS} — tick"
  sleep "${INTERVAL}"
done
