#!/bin/bash
# update.sh — Update kiro proxy source from GitHub.
#
# Downloads the latest proxy.py and compress.py without touching
# certificates, shell config, or the launchd service.
# The proxy auto-reloads on next request (mitmproxy re-imports the script).
#
# Usage:
#   ~/.kiro-proxy/update.sh

set -euo pipefail

GITHUB_RAW="https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main"
PROXY_DIR="${HOME}/.kiro-proxy"
PROXY_PORT=9090

# Colors
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'; YELLOW='\033[0;33m'; BOLD='\033[1m'; NC='\033[0m'
else
    GREEN='' YELLOW='' BOLD='' NC=''
fi

info()  { echo -e "${GREEN}✓${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC} $*"; }

echo -e "${BOLD}=== Kiro Proxy — Update ===${NC}"
echo ""

if [[ ! -d "${PROXY_DIR}/src" ]]; then
    echo "Error: ${PROXY_DIR}/src not found. Run the installer first."
    exit 1
fi

download() {
    local url="$1" dest="$2"
    if curl -sSL --fail "${url}" -o "${dest}"; then
        return 0
    else
        echo "Failed to download: ${url}" >&2
        return 1
    fi
}

# Download latest source
echo "Downloading latest source..."
download "${GITHUB_RAW}/src/proxy.py"     "${PROXY_DIR}/src/proxy.py"
download "${GITHUB_RAW}/src/compress.py"  "${PROXY_DIR}/src/compress.py"
info "Updated proxy source"

# Update scripts too
download "${GITHUB_RAW}/scripts/kiro-wrapper.sh" "${PROXY_DIR}/kiro-wrapper.sh"
download "${GITHUB_RAW}/scripts/doctor.sh"       "${PROXY_DIR}/doctor.sh"
download "${GITHUB_RAW}/scripts/update.sh"       "${PROXY_DIR}/update.sh"
download "${GITHUB_RAW}/scripts/uninstall.sh"    "${PROXY_DIR}/uninstall.sh"
chmod +x "${PROXY_DIR}/kiro-wrapper.sh" \
         "${PROXY_DIR}/doctor.sh" \
         "${PROXY_DIR}/update.sh" \
         "${PROXY_DIR}/uninstall.sh"
info "Updated scripts"

# Restart proxy to pick up new code
echo ""
echo "Restarting proxy..."
PLIST="${HOME}/Library/LaunchAgents/com.kiro-proxy.compression.plist"
if [[ -f "${PLIST}" ]]; then
    launchctl unload "${PLIST}" 2>/dev/null || true
    sleep 1
    launchctl load "${PLIST}"
    sleep 2
    if curl -s --max-time 2 "http://127.0.0.1:${PROXY_PORT}/health" > /dev/null 2>&1; then
        info "Proxy restarted and healthy"
    else
        warn "Proxy not responding after restart. Check: tail -20 ${PROXY_DIR}/logs/proxy.err"
    fi
else
    warn "No launchd plist found — restart proxy manually"
fi

echo ""
echo "Done. Latest compression logic is now active."
