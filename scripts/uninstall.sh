#!/bin/bash
# uninstall.sh — Remove the kiro compression proxy completely.
#
# Reverses everything install.sh did:
#   1. Stops and removes the launchd service
#   2. Removes env vars from shell config
#   3. Removes env vars from tao config
#   4. Clears launchctl env vars
#   5. Deletes ~/.kiro-proxy/
#
# After uninstall, kiro-cli works exactly as before (direct to AWS).
#
# Usage:
#   ~/.kiro-proxy/uninstall.sh

set -euo pipefail

PROXY_DIR="${HOME}/.kiro-proxy"
PLIST_NAME="com.kiro-proxy.compression"
PLIST_DEST="${HOME}/Library/LaunchAgents/${PLIST_NAME}.plist"
SHELL_RC="${HOME}/.zshrc"
[[ "${SHELL}" == *bash* ]] && SHELL_RC="${HOME}/.bashrc"

# Colors
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'
    BOLD='\033[1m'; NC='\033[0m'
else
    GREEN='' YELLOW='' RED='' BOLD='' NC=''
fi

info()  { echo -e "${GREEN}✓${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC} $*"; }

echo -e "${BOLD}=== Kiro Proxy — Uninstall ===${NC}"
echo ""
echo "This will remove the kiro compression proxy and restore"
echo "kiro-cli to its default behavior (direct to AWS)."
echo ""

# Confirmation
read -rp "Proceed? [y/N] " REPLY
if [[ ! "${REPLY}" =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi

echo ""

# 1. Stop and remove launchd service
if launchctl list "${PLIST_NAME}" &>/dev/null; then
    launchctl unload "${PLIST_DEST}" 2>/dev/null || true
    info "Stopped proxy service"
fi
rm -f "${PLIST_DEST}"
info "Removed launchd plist"

# Kill any lingering proxy processes
pkill -f "mitmdump.*9090" 2>/dev/null || true

# 2. Remove env vars from shell config
if [[ -f "${SHELL_RC}" ]]; then
    if grep -q "kiro-proxy" "${SHELL_RC}" 2>/dev/null; then
        sed -i '' '/# >>> kiro-proxy >>>/,/# <<< kiro-proxy <<</d' "${SHELL_RC}"
        # Also clean up any standalone proxy lines from older installs
        sed -i '' '/HTTPS_PROXY.*127.0.0.1:9090/d' "${SHELL_RC}"
        sed -i '' '/SSL_CERT_FILE.*kiro-proxy/d' "${SHELL_RC}"
        sed -i '' '/alias kiro-cli.*kiro-proxy/d' "${SHELL_RC}"
        # Remove empty lines left behind (collapse doubles)
        sed -i '' '/^$/N;/^\n$/d' "${SHELL_RC}"
        info "Removed proxy config from ${SHELL_RC}"
    else
        info "No proxy config found in ${SHELL_RC}"
    fi
fi

# 3. Remove from tao env
TAO_ENV="${HOME}/.tao/env"
if [[ -f "${TAO_ENV}" ]]; then
    if grep -q "kiro-proxy\|HTTPS_PROXY\|SSL_CERT_FILE.*kiro-proxy" "${TAO_ENV}" 2>/dev/null; then
        sed -i '' '/# Kiro compression proxy/d' "${TAO_ENV}"
        sed -i '' '/HTTPS_PROXY.*127.0.0.1:9090/d' "${TAO_ENV}"
        sed -i '' '/SSL_CERT_FILE.*kiro-proxy/d' "${TAO_ENV}"
        sed -i '' '/^$/N;/^\n$/d' "${TAO_ENV}"
        info "Removed proxy config from ${TAO_ENV}"
    fi
fi

# 4. Clear launchctl env vars
launchctl setenv HTTPS_PROXY "" 2>/dev/null || true
# Can't truly unset via launchctl, but empty string effectively disables
# The vars will be gone after reboot anyway
info "Cleared launchctl env vars (fully gone after reboot)"

# 5. Remove CLI from PATH
CLI_DEST="/usr/local/bin/kiro-proxy"
if [[ -L "${CLI_DEST}" ]]; then
    if [[ -w "${CLI_DEST}" ]] || [[ -w "$(dirname "${CLI_DEST}")" ]]; then
        rm -f "${CLI_DEST}"
    else
        sudo rm -f "${CLI_DEST}" 2>/dev/null || true
    fi
    info "Removed kiro-proxy CLI from PATH"
fi

# 6. Delete ~/.kiro-proxy/
if [[ -d "${PROXY_DIR}" ]]; then
    rm -rf "${PROXY_DIR}"
    info "Deleted ${PROXY_DIR}"
fi

echo ""
echo -e "${BOLD}Uninstall complete.${NC}"
echo ""
echo "  kiro-cli now connects directly to AWS (no proxy)."
echo "  Open a new terminal for shell changes to take effect."
echo "  Reboot to fully clear launchctl env vars."
echo ""
echo "  To reinstall:"
echo "  curl -sSL --noproxy '*' https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main/scripts/install.sh | bash"
