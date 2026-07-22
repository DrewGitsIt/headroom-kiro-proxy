#!/bin/bash
# uninstall.sh — Remove the kiro compression proxy completely.
#
# Reverses everything install.sh did:
#   1. Stops and removes both LaunchAgents (proxy + applet)
#   2. Removes env vars from shell config (global AND wrapper mode)
#   3. Clears launchctl env vars
#   4. Removes CLI from PATH (~/.local/bin)
#   5. Deletes ~/.kiro-proxy/ (including venv)
#
# After uninstall, kiro-cli works exactly as before (direct to AWS).

set -uo pipefail

PROXY_DIR="${HOME}/.kiro-proxy"
PLIST_LABEL="com.kiro-proxy.compression"
PLIST_DEST="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"
APPLET_PLIST_LABEL="com.kiro-proxy.applet"
APPLET_PLIST_DEST="${HOME}/Library/LaunchAgents/${APPLET_PLIST_LABEL}.plist"

# Determine shell config file (guard against unset SHELL in minimal envs)
case "${SHELL:-/bin/zsh}" in
    *bash*) SHELL_RC="${HOME}/.bashrc" ;;
    *)      SHELL_RC="${HOME}/.zshrc"  ;;
esac

# Colors
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'
    BOLD='\033[1m'; NC='\033[0m'
else
    GREEN=''; YELLOW=''; RED=''; BOLD=''; NC=''
fi

info() { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }

echo -e "${BOLD}kiro-proxy uninstaller${NC}"
echo ""

# 1. Stop and remove LaunchAgents
if launchctl list "${PLIST_LABEL}" &>/dev/null; then
    launchctl unload "${PLIST_DEST}" 2>/dev/null || true
    info "Stopped proxy service"
fi
rm -f "${PLIST_DEST}"

if launchctl list "${APPLET_PLIST_LABEL}" &>/dev/null; then
    launchctl unload "${APPLET_PLIST_DEST}" 2>/dev/null || true
    info "Stopped applet service"
fi
rm -f "${APPLET_PLIST_DEST}"

# Also clean up legacy plist names if they exist
for legacy in "ai.headroom.kiro-connect-proxy" "ai.headroom.kiro-applet"; do
    legacy_plist="${HOME}/Library/LaunchAgents/${legacy}.plist"
    if [[ -f "${legacy_plist}" ]]; then
        launchctl unload "${legacy_plist}" 2>/dev/null || true
        rm -f "${legacy_plist}"
        info "Removed legacy service ${legacy}"
    fi
done

info "Removed LaunchAgents"

# 2. Remove env vars from shell config — handles both global and wrapper mode
if grep -q "# >>> kiro-proxy >>>" "${SHELL_RC}" 2>/dev/null; then
    sed -i '' '/# >>> kiro-proxy >>>/,/# <<< kiro-proxy <<</d' "${SHELL_RC}"
    info "Removed proxy env vars (global mode) from ${SHELL_RC}"
fi

if grep -q "# >>> kiro-proxy-wrapper >>>" "${SHELL_RC}" 2>/dev/null; then
    sed -i '' '/# >>> kiro-proxy-wrapper >>>/,/# <<< kiro-proxy-wrapper <<</d' "${SHELL_RC}"
    info "Removed kiro-cli alias (wrapper mode) from ${SHELL_RC}"
fi

# Remove PATH export for ~/.local/bin if we added it
if grep -q "# kiro-proxy: add ~/.local/bin" "${SHELL_RC}" 2>/dev/null; then
    sed -i '' '/# kiro-proxy: add ~\/.local\/bin/d' "${SHELL_RC}"
    sed -i '' '\|export PATH="${HOME}/\.local/bin|d' "${SHELL_RC}"
    info "Removed PATH export from ${SHELL_RC}"
fi

if ! grep -q "kiro-proxy" "${SHELL_RC}" 2>/dev/null; then
    info "Shell config clean (no kiro-proxy references remain)"
fi

# 2b. Remove from tao env (if present)
TAO_ENV="${HOME}/.tao/env"
if [[ -f "${TAO_ENV}" ]] && grep -q "Kiro compression proxy" "${TAO_ENV}" 2>/dev/null; then
    sed -i '' '/# Kiro compression proxy/d' "${TAO_ENV}"
    sed -i '' '/HTTPS_PROXY.*127.0.0.1:9090/d' "${TAO_ENV}"
    sed -i '' '/SSL_CERT_FILE.*kiro-proxy/d' "${TAO_ENV}"
    sed -i '' '/NODE_EXTRA_CA_CERTS.*kiro-proxy/d' "${TAO_ENV}"
    info "Removed proxy config from ~/.tao/env"
fi

# 3. Clear launchctl env vars
launchctl unsetenv HTTPS_PROXY 2>/dev/null || true
launchctl unsetenv SSL_CERT_FILE 2>/dev/null || true
launchctl unsetenv NODE_EXTRA_CA_CERTS 2>/dev/null || true
info "Cleared launchctl environment variables"

# 4. Remove CLI symlink from ~/.local/bin
CLI_DEST="${HOME}/.local/bin/kiro-proxy"
if [[ -L "${CLI_DEST}" || -f "${CLI_DEST}" ]]; then
    rm -f "${CLI_DEST}"
    info "Removed kiro-proxy CLI from ~/.local/bin"
fi
# Also check legacy location (/usr/local/bin) from older installs
LEGACY_CLI="/usr/local/bin/kiro-proxy"
if [[ -L "${LEGACY_CLI}" ]]; then
    if [[ -w "$(dirname "${LEGACY_CLI}")" ]]; then
        rm -f "${LEGACY_CLI}"
    else
        sudo rm -f "${LEGACY_CLI}" 2>/dev/null || true
    fi
    info "Removed legacy CLI from /usr/local/bin"
fi

# 5. Delete ~/.kiro-proxy/ (includes venv, source, certs, logs)
if [[ -d "${PROXY_DIR}" ]]; then
    rm -rf "${PROXY_DIR}"
    info "Deleted ${PROXY_DIR}"
fi

echo ""
echo -e "${BOLD}Uninstall complete.${NC}"
echo ""
echo "  kiro-cli now connects directly to AWS (no compression proxy)."
echo "  Open a new terminal for shell changes to take effect."
echo ""
echo "  To reinstall:"
echo "  curl -sSL --noproxy '*' https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main/scripts/install.sh | bash"
