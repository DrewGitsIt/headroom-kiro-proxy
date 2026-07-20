#!/bin/bash
# update.sh — Update the kiro compression proxy to the latest version.
#
# Re-downloads source files and upgrades headroom-ai, then restarts the proxy.

set -euo pipefail

PROXY_DIR="${HOME}/.kiro-proxy"
VENV_DIR="${PROXY_DIR}/.venv"
PROXY_PORT=9090
GITHUB_RAW="https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main"
PLIST_LABEL="com.kiro-proxy.compression"
PLIST_DEST="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"
APPLET_PLIST_LABEL="com.kiro-proxy.applet"
APPLET_PLIST_DEST="${HOME}/Library/LaunchAgents/${APPLET_PLIST_LABEL}.plist"
HEALTH_URL="http://127.0.0.1:${PROXY_PORT}/health"

# Colors
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'
    BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'
else
    GREEN=''; YELLOW=''; RED=''; BOLD=''; DIM=''; NC=''
fi

info() { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
fail() { echo -e "${RED}✗${NC} $*"; exit 1; }

download() {
    local url="$1" dest="$2"
    if ! curl -sSL --noproxy '*' --fail "${url}" -o "${dest}"; then
        fail "Failed to download: ${url}"
    fi
}

echo -e "${BOLD}kiro-proxy updater${NC}"
echo ""

if [[ ! -d "${PROXY_DIR}" ]]; then
    fail "${PROXY_DIR} not found. Run the installer first."
fi

if [[ ! -d "${VENV_DIR}" ]]; then
    fail "Venv not found at ${VENV_DIR}. Run the installer first."
fi

# 1. Upgrade headroom-ai
echo -e "${DIM}  Upgrading headroom-ai...${NC}"
"${VENV_DIR}/bin/pip" install --quiet --upgrade "headroom-ai" "rumps>=0.4.0" 2>&1 | grep -v "already satisfied" || true
info "Upgraded headroom-ai"

# 2. Download latest source
download "${GITHUB_RAW}/src/connect_proxy.py" "${PROXY_DIR}/src/connect_proxy.py"
download "${GITHUB_RAW}/src/handler.py"       "${PROXY_DIR}/src/handler.py"
download "${GITHUB_RAW}/src/applet.py"        "${PROXY_DIR}/src/applet.py"
download "${GITHUB_RAW}/scripts/kiro-proxy"   "${PROXY_DIR}/kiro-proxy"
download "${GITHUB_RAW}/scripts/doctor.sh"    "${PROXY_DIR}/doctor.sh"
download "${GITHUB_RAW}/scripts/update.sh"    "${PROXY_DIR}/update.sh"
download "${GITHUB_RAW}/scripts/uninstall.sh" "${PROXY_DIR}/uninstall.sh"

chmod +x "${PROXY_DIR}/kiro-proxy" \
         "${PROXY_DIR}/doctor.sh" \
         "${PROXY_DIR}/update.sh" \
         "${PROXY_DIR}/uninstall.sh"

info "Downloaded latest source"

# 3. Restart services
launchctl unload "${PLIST_DEST}" 2>/dev/null || true
launchctl unload "${APPLET_PLIST_DEST}" 2>/dev/null || true
sleep 1
launchctl load "${PLIST_DEST}" 2>/dev/null || true
launchctl load "${APPLET_PLIST_DEST}" 2>/dev/null || true

# Wait for proxy
sleep 2
if curl -s --max-time 2 "${HEALTH_URL}" > /dev/null 2>&1; then
    info "Proxy restarted successfully"
else
    warn "Proxy not responding after restart. Check: kiro-proxy logs"
fi

echo ""
info "Update complete. Compression active."
