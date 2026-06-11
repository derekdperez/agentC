#!/usr/bin/env bash
#
# Install agentC as supervised systemd *user* services:
#
#   agentc-engine.service     the worker (schedules, file watchers, agents)
#   agentc-dashboard.service  the always-on web UI + control plane (agentc serve)
#
# Both use Restart=always, so a crash is brought back automatically. They are
# independent units, so the dashboard survives an engine crash (and hosts the
# Start/Stop/Restart buttons that control the engine).
#
# Usage:   scripts/install_service.sh [--port N]
# Then:    open http://127.0.0.1:8765/  and use the engine on/off buttons.
#
set -euo pipefail

PORT=8765
[ "${1:-}" = "--port" ] && PORT="${2:?--port needs a value}"

# Resolve the project root (parent of this scripts/ dir), following symlinks.
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$SELF")/.." && pwd)"
PY="$(command -v python3)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

echo "agentC root : $ROOT"
echo "python      : $PY"
echo "unit dir    : $UNIT_DIR"
echo "dashboard   : http://127.0.0.1:$PORT/"
mkdir -p "$UNIT_DIR"

# Pass the *current* PATH through so agent CLIs (opencode, claude, …) resolve
# inside the service, where systemd otherwise provides only a minimal PATH.
SERVICE_PATH="$PATH"

write_unit() {
  local name="$1" desc="$2" cmd="$3"
  cat > "$UNIT_DIR/$name" <<EOF
[Unit]
Description=$desc
After=default.target
# Never stop retrying after a crash loop — always bring it back.
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=AGENTC_ROOT=$ROOT
Environment=PATH=$SERVICE_PATH
ExecStart=$cmd
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
  echo "wrote $UNIT_DIR/$name"
}

write_unit agentc-engine.service \
  "agentC workflow engine (worker)" \
  "$PY -m agentc start"

write_unit agentc-dashboard.service \
  "agentC dashboard (web UI + control plane)" \
  "$PY -m agentc serve --host 127.0.0.1 --port $PORT"

# Let the user manager survive logout so the services keep running.
if loginctl enable-linger "$USER" 2>/dev/null; then
  echo "linger enabled for $USER (services survive logout)"
else
  echo "NOTE: could not enable linger (needs privileges). Services run while you"
  echo "      are logged in. To make them survive logout, run:"
  echo "        sudo loginctl enable-linger $USER"
fi

systemctl --user daemon-reload
systemctl --user enable --now agentc-dashboard.service
systemctl --user enable --now agentc-engine.service

echo
echo "Done. Status:"
systemctl --user --no-pager --output=short status \
  agentc-engine.service agentc-dashboard.service 2>/dev/null | \
  grep -E "Loaded:|Active:" || true
echo
echo "Open http://127.0.0.1:$PORT/  — use the green/red engine buttons in the header."
