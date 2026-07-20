#!/bin/bash
# install.sh — Install the kiro compression proxy.
#
# Usage:
#   curl -sSL --noproxy '*' https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main/scripts/install.sh | bash
#
# What this does:
#   1. Downloads proxy source from GitHub
#   2. Generates a local CA certificate (per-process trust via SSL_CERT_FILE)
#   3. Installs a launchd plist for auto-start with keepalive
#   4. Configures shell env vars (HTTPS_PROXY + SSL_CERT_FILE)
#   5. Optionally configures ~/.tao/env for ACP sessions
#   6. Runs a health check to verify everything works
#
# What this does NOT do:
#   - Modify system keychain (no admin privileges needed)
#   - Install system-wide proxy settings (only in your shell profile)
#   - Affect any tool other than kiro-cli (--allow-hosts scoping)
#
# To uninstall:
#   ~/.kiro-proxy/uninstall.sh

set -euo pipefail

# --- Configuration ---
GITHUB_RAW="https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main"
PROXY_DIR="${HOME}/.kiro-proxy"
PROXY_PORT=9090
PLIST_NAME="com.kiro-proxy.compression"
PLIST_DEST="${HOME}/Library/LaunchAgents/${PLIST_NAME}.plist"

# Colors (if terminal supports them)
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    RED='\033[0;31m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    GREEN='' YELLOW='' RED='' BOLD='' NC=''
fi

info()  { echo -e "${GREEN}✓${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC} $*"; }
fail()  { echo -e "${RED}✗${NC} $*"; exit 1; }
step()  { echo -e "\n${BOLD}[$1/6]${NC} $2"; }

echo -e "${BOLD}=== Kiro Compression Proxy — Install ===${NC}"
echo ""
echo "This will install a local HTTPS proxy that compresses kiro-cli"
echo "conversation history, reducing token costs by ~50%."
echo ""
echo "Install location: ${PROXY_DIR}"
echo "Proxy address:    127.0.0.1:${PROXY_PORT}"
echo ""

# --- Pre-flight checks ---
MITMDUMP="$(command -v mitmdump 2>/dev/null || true)"
if [[ -z "${MITMDUMP}" ]]; then
    echo "mitmdump not found. Installing mitmproxy via Homebrew..."
    if command -v brew &>/dev/null; then
        brew install mitmproxy
        MITMDUMP="$(command -v mitmdump)"
    else
        fail "mitmdump not found and Homebrew not available. Install mitmproxy first:\n  brew install mitmproxy"
    fi
fi
info "Found mitmdump at ${MITMDUMP}"

if ! command -v openssl &>/dev/null; then
    fail "openssl not found. Cannot generate certificates."
fi

if ! command -v curl &>/dev/null; then
    fail "curl not found."
fi

# --- Step 1: Download proxy source ---
step 1 "Downloading proxy source from GitHub"

mkdir -p "${PROXY_DIR}/src" "${PROXY_DIR}/logs" "${PROXY_DIR}/scripts"

download() {
    local url="$1" dest="$2"
    if ! curl -sSL --noproxy '*' --fail "${url}" -o "${dest}"; then
        fail "Failed to download: ${url}"
    fi
}

download "${GITHUB_RAW}/src/proxy.py"           "${PROXY_DIR}/src/proxy.py"
download "${GITHUB_RAW}/src/compress.py"        "${PROXY_DIR}/src/compress.py"
download "${GITHUB_RAW}/src/kiro_translator.py" "${PROXY_DIR}/src/kiro_translator.py"
download "${GITHUB_RAW}/scripts/kiro-wrapper.sh" "${PROXY_DIR}/kiro-wrapper.sh"
download "${GITHUB_RAW}/scripts/kiro-proxy"      "${PROXY_DIR}/kiro-proxy"
download "${GITHUB_RAW}/scripts/doctor.sh"       "${PROXY_DIR}/doctor.sh"
download "${GITHUB_RAW}/scripts/update.sh"       "${PROXY_DIR}/update.sh"
download "${GITHUB_RAW}/scripts/uninstall.sh"    "${PROXY_DIR}/uninstall.sh"

chmod +x "${PROXY_DIR}/kiro-wrapper.sh" \
         "${PROXY_DIR}/kiro-proxy" \
         "${PROXY_DIR}/doctor.sh" \
         "${PROXY_DIR}/update.sh" \
         "${PROXY_DIR}/uninstall.sh"

# Install CLI to PATH
CLI_DEST="/usr/local/bin/kiro-proxy"
if [[ -w "/usr/local/bin" ]]; then
    ln -sf "${PROXY_DIR}/kiro-proxy" "${CLI_DEST}"
    info "Installed kiro-proxy CLI to ${CLI_DEST}"
elif [[ -w "$(dirname "${CLI_DEST}")" ]]; then
    ln -sf "${PROXY_DIR}/kiro-proxy" "${CLI_DEST}"
    info "Installed kiro-proxy CLI to ${CLI_DEST}"
else
    # Try with sudo, but don't fail the install if the user declines
    echo "Installing kiro-proxy CLI to ${CLI_DEST} (requires sudo)..."
    if sudo ln -sf "${PROXY_DIR}/kiro-proxy" "${CLI_DEST}" 2>/dev/null; then
        info "Installed kiro-proxy CLI to ${CLI_DEST}"
    else
        warn "Could not install CLI to PATH. Use directly: ${PROXY_DIR}/kiro-proxy"
    fi
fi

info "Downloaded to ${PROXY_DIR}/src/"

# --- Step 2: Generate CA certificate ---
step 2 "Generating CA certificate"

CA_KEY="${PROXY_DIR}/ca.key"
CA_PEM="${PROXY_DIR}/ca.pem"
SYSTEM_ROOTS="${PROXY_DIR}/system-roots.pem"
CA_BUNDLE="${PROXY_DIR}/ca-bundle.pem"
MITM_CA="${PROXY_DIR}/mitmproxy-ca.pem"

if [[ -f "${CA_KEY}" && -f "${CA_PEM}" ]]; then
    info "CA already exists, skipping generation"
else
    openssl req -x509 -newkey rsa:2048 \
        -keyout "${CA_KEY}" \
        -out "${CA_PEM}" \
        -days 3650 \
        -nodes \
        -subj "/CN=Kiro Compression Proxy CA/O=kiro-proxy/OU=local" \
        2>/dev/null
    chmod 600 "${CA_KEY}"
    info "Generated CA certificate (10-year validity)"
fi

# Combined key+cert for mitmproxy
cat "${CA_KEY}" "${CA_PEM}" > "${MITM_CA}"
chmod 600 "${MITM_CA}"

# Remove stale mitmproxy auto-generated artifacts
rm -f "${PROXY_DIR}/mitmproxy-ca-cert.pem" "${PROXY_DIR}/mitmproxy-ca-cert.cer" \
      "${PROXY_DIR}/mitmproxy-ca.p12" "${PROXY_DIR}/mitmproxy-ca-cert.p12" \
      "${PROXY_DIR}/mitmproxy-dhparam.pem"

# Export system root CAs and build combined bundle
security export -t certs -f pemseq \
    -k /System/Library/Keychains/SystemRootCertificates.keychain \
    -o "${SYSTEM_ROOTS}" 2>/dev/null

cat "${SYSTEM_ROOTS}" "${CA_PEM}" > "${CA_BUNDLE}"
info "Built CA bundle (system roots + proxy CA)"

# --- Step 3: Install launchd plist ---
step 3 "Installing launchd service"

# Stop existing service if running
if launchctl list "${PLIST_NAME}" &>/dev/null; then
    launchctl unload "${PLIST_DEST}" 2>/dev/null || true
fi

mkdir -p "$(dirname "${PLIST_DEST}")"

cat > "${PLIST_DEST}" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${MITMDUMP}</string>
        <string>-s</string>
        <string>${PROXY_DIR}/src/proxy.py</string>
        <string>--listen-port</string>
        <string>${PROXY_PORT}</string>
        <string>--set</string>
        <string>confdir=${PROXY_DIR}</string>
        <string>--allow-hosts</string>
        <string>runtime\\.us-east-1\\.kiro\\.dev</string>
        <string>-q</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${PROXY_DIR}/src</string>

    <key>KeepAlive</key>
    <true/>

    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>${PROXY_DIR}/logs/proxy.log</string>

    <key>StandardErrorPath</key>
    <string>${PROXY_DIR}/logs/proxy.err</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>

    <key>ThrottleInterval</key>
    <integer>1</integer>
</dict>
</plist>
EOF

launchctl load "${PLIST_DEST}"

# Wait for proxy to come up (up to 5 seconds)
RETRIES=5
PROXY_UP=false
for i in $(seq 1 ${RETRIES}); do
    sleep 1
    if curl -s --max-time 2 "http://127.0.0.1:${PROXY_PORT}/health" > /dev/null 2>&1; then
        PROXY_UP=true
        break
    fi
done

if [[ "${PROXY_UP}" == "true" ]]; then
    info "Proxy is running on 127.0.0.1:${PROXY_PORT}"
else
    warn "Proxy not responding after ${RETRIES}s. Checking for errors..."
    echo ""
    if [[ -f "${PROXY_DIR}/logs/proxy.err" ]]; then
        LAST_ERR="$(tail -5 "${PROXY_DIR}/logs/proxy.err" 2>/dev/null)"
        if [[ -n "${LAST_ERR}" ]]; then
            echo "  Last error output:"
            echo "  ${LAST_ERR}" | sed 's/^/    /'
        fi
    fi
    echo ""
    warn "Try: tail -20 ${PROXY_DIR}/logs/proxy.err"
    warn "Then re-run: ~/.kiro-proxy/doctor.sh"
fi

# --- Step 4: Configure shell environment ---
step 4 "Configuring shell environment"

SHELL_RC="${HOME}/.zshrc"
if [[ "${SHELL}" == *bash* ]]; then
    SHELL_RC="${HOME}/.bashrc"
fi

SHELL_BLOCK='# >>> kiro-proxy >>>
export HTTPS_PROXY="http://127.0.0.1:9090"
export SSL_CERT_FILE="${HOME}/.kiro-proxy/ca-bundle.pem"
# <<< kiro-proxy <<<'

if grep -q "kiro-proxy" "${SHELL_RC}" 2>/dev/null; then
    # Replace existing block
    sed -i '' '/# >>> kiro-proxy >>>/,/# <<< kiro-proxy <<</d' "${SHELL_RC}"
fi

echo "" >> "${SHELL_RC}"
echo "${SHELL_BLOCK}" >> "${SHELL_RC}"
info "Added HTTPS_PROXY + SSL_CERT_FILE to ${SHELL_RC}"

# Set for current GUI session (ephemeral — gone on reboot, but plist handles restarts)
launchctl setenv HTTPS_PROXY "http://127.0.0.1:${PROXY_PORT}" 2>/dev/null || true
launchctl setenv SSL_CERT_FILE "${CA_BUNDLE}" 2>/dev/null || true
info "Set env vars for current session (launchctl setenv)"

# --- Step 5: Configure tao (if present) ---
step 5 "Checking for tao integration"

TAO_ENV="${HOME}/.tao/env"
if [[ -d "${HOME}/.tao" ]]; then
    if grep -q "HTTPS_PROXY" "${TAO_ENV}" 2>/dev/null; then
        info "tao/env already has proxy config"
    else
        echo "" >> "${TAO_ENV}"
        echo "# Kiro compression proxy" >> "${TAO_ENV}"
        echo "HTTPS_PROXY=http://127.0.0.1:${PROXY_PORT}" >> "${TAO_ENV}"
        echo "SSL_CERT_FILE=${CA_BUNDLE}" >> "${TAO_ENV}"
        info "Added proxy config to ${TAO_ENV} (covers ACP sessions)"
        warn "Restart tao-dashboard for ACP sessions to pick this up"
    fi
else
    info "tao not detected, skipping"
fi

# --- Step 6: Run doctor.sh ---
step 6 "Running health check"

echo ""
bash "${PROXY_DIR}/doctor.sh"

echo ""
echo -e "${BOLD}=== Installation complete ===${NC}"
echo ""
echo "  Proxy:     127.0.0.1:${PROXY_PORT} (running)"
echo "  CA cert:   ${CA_BUNDLE}"
echo "  Logs:      ${PROXY_DIR}/logs/"
echo ""
echo "  To activate in this shell:  source ${SHELL_RC}"
echo ""
echo "  kiro-proxy status      Show health and compression stats"
echo "  kiro-proxy logs        Tail proxy logs"
echo "  kiro-proxy disable     Temporarily stop compression"
echo "  kiro-proxy enable      Re-enable compression"
echo "  kiro-proxy update      Pull latest compression logic"
echo "  kiro-proxy uninstall   Clean removal"
echo ""
echo "  All kiro-cli sessions (terminal, ACP, IDE) will now be compressed."
echo "  Other tools (git, curl, npm, brew, AWS) are unaffected."
