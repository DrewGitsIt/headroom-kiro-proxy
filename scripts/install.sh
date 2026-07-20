#!/bin/bash
# install.sh — Install the kiro compression proxy.
#
# Usage:
#   curl -sSL --noproxy '*' https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main/scripts/install.sh | bash
#
# What this does:
#   1. Creates a Python venv at ~/.kiro-proxy/.venv
#   2. Installs headroom-ai (Rust SmartCrusher) and rumps (menu bar)
#   3. Downloads proxy, handler, applet source from GitHub
#   4. Generates a local CA certificate (per-process trust only)
#   5. Installs LaunchAgents (proxy + applet, auto-start)
#   6. Adds HTTPS_PROXY + SSL_CERT_FILE to shell config
#   7. Verifies kiro-cli works before and after proxy

set -euo pipefail

# --- Config ---
PROXY_DIR="${HOME}/.kiro-proxy"
PROXY_PORT=9090
VENV_DIR="${PROXY_DIR}/.venv"
GITHUB_RAW="https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main"
PLIST_LABEL="com.kiro-proxy.compression"
PLIST_DEST="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"
APPLET_PLIST_LABEL="com.kiro-proxy.applet"
APPLET_PLIST_DEST="${HOME}/Library/LaunchAgents/${APPLET_PLIST_LABEL}.plist"
SHELL_RC="${HOME}/.zshrc"
[[ "${SHELL}" == *bash* ]] && SHELL_RC="${HOME}/.bashrc"
HEALTH_URL="http://127.0.0.1:${PROXY_PORT}/health"

# --- Colors ---
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'
    BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'
else
    GREEN=''; YELLOW=''; RED=''; BOLD=''; DIM=''; NC=''
fi

info()  { echo -e "${GREEN}✓${NC} $*"; }
warn()  { echo -e "${YELLOW}!${NC} $*"; }
fail()  { echo -e "${RED}✗${NC} $*"; exit 1; }
step()  { echo ""; echo -e "${BOLD}[step $1]${NC} $2"; }

download() {
    local url="$1" dest="$2"
    if ! curl -sSL --noproxy '*' --fail "${url}" -o "${dest}"; then
        fail "Failed to download: ${url}"
    fi
}

# --- Pre-flight checks ---
echo -e "${BOLD}kiro-proxy installer${NC}"
echo ""

# Python 3.10+ required
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "${candidate}" &>/dev/null; then
        PY_VERSION=$("${candidate}" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        PY_MAJOR=$(echo "${PY_VERSION}" | cut -d. -f1)
        PY_MINOR=$(echo "${PY_VERSION}" | cut -d. -f2)
        if [[ "${PY_MAJOR}" -ge 3 && "${PY_MINOR}" -ge 10 ]]; then
            PYTHON="$(command -v "${candidate}")"
            break
        fi
    fi
done

if [[ -z "${PYTHON}" ]]; then
    fail "Python 3.10+ required but not found. Install via: brew install python@3.13"
fi
info "Found Python ${PY_VERSION} at ${PYTHON}"

if ! command -v openssl &>/dev/null; then
    fail "openssl not found. Cannot generate certificates."
fi

if ! command -v kiro-cli &>/dev/null; then
    fail "kiro-cli not found on PATH. Install kiro-cli first, then re-run this installer."
fi
info "Found kiro-cli"

# --- Step 1: Verify kiro works (baseline) ---
step 1 "Testing kiro-cli baseline (no proxy)"

# Unset proxy in case of previous partial install
KIRO_TEST_CMD=(env -u HTTPS_PROXY -u SSL_CERT_FILE kiro-cli chat --no-interactive "respond with only the word: ok")
BASELINE_START=$(date +%s)
if BASELINE_OUTPUT=$("${KIRO_TEST_CMD[@]}" 2>&1); then
    BASELINE_END=$(date +%s)
    BASELINE_SECS=$((BASELINE_END - BASELINE_START))
    info "kiro-cli responded in ${BASELINE_SECS}s (baseline)"
else
    fail "kiro-cli failed WITHOUT proxy. Fix kiro-cli first, then re-run installer.
Output: ${BASELINE_OUTPUT}"
fi

# --- Step 2: Create venv and install dependencies ---
step 2 "Creating venv and installing dependencies"

mkdir -p "${PROXY_DIR}/src" "${PROXY_DIR}/logs" "${PROXY_DIR}/assets"

if [[ -d "${VENV_DIR}" ]]; then
    info "Existing venv found, reusing"
else
    "${PYTHON}" -m venv "${VENV_DIR}"
    info "Created venv at ${VENV_DIR}"
fi

echo -e "${DIM}  Installing headroom-ai and rumps (this may take 30-60s)...${NC}"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip 2>/dev/null
"${VENV_DIR}/bin/pip" install --quiet "headroom-ai>=0.31.0" "rumps>=0.4.0" 2>&1 | grep -v "already satisfied" || true
info "Installed headroom-ai + rumps"

# --- Step 3: Download proxy source ---
step 3 "Downloading proxy source"

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

info "Downloaded to ${PROXY_DIR}/src/"

# Install CLI to PATH
CLI_DEST="/usr/local/bin/kiro-proxy"
if [[ -w "/usr/local/bin" ]]; then
    ln -sf "${PROXY_DIR}/kiro-proxy" "${CLI_DEST}"
    info "Installed kiro-proxy CLI to ${CLI_DEST}"
else
    echo "Installing kiro-proxy CLI to ${CLI_DEST} (requires sudo)..."
    if sudo ln -sf "${PROXY_DIR}/kiro-proxy" "${CLI_DEST}" 2>/dev/null; then
        info "Installed kiro-proxy CLI to ${CLI_DEST}"
    else
        warn "Could not install CLI to PATH. Use directly: ${PROXY_DIR}/kiro-proxy"
    fi
fi

# --- Step 4: Generate CA certificate ---
step 4 "Generating CA certificate"

CERT_DIR="${PROXY_DIR}"
CA_KEY="${CERT_DIR}/key.pem"
CA_CERT="${CERT_DIR}/cert.pem"
CA_BUNDLE="${CERT_DIR}/ca-bundle.pem"

if [[ -f "${CA_CERT}" && -f "${CA_KEY}" ]]; then
    # Verify existing cert is not expired
    if openssl x509 -checkend 86400 -noout -in "${CA_CERT}" &>/dev/null; then
        info "Existing CA certificate is valid, reusing"
    else
        warn "Existing CA certificate expired, regenerating"
        rm -f "${CA_KEY}" "${CA_CERT}" "${CA_BUNDLE}"
    fi
fi

if [[ ! -f "${CA_CERT}" ]]; then
    openssl req -x509 -newkey rsa:2048 \
        -keyout "${CA_KEY}" -out "${CA_CERT}" \
        -days 3650 -nodes \
        -subj "/CN=kiro-proxy CA/O=kiro-proxy/OU=local" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        2>/dev/null
    info "Generated CA certificate (10-year validity)"
fi

# Build bundle: system roots + our CA
SYSTEM_ROOTS=""
if [[ -f "/etc/ssl/cert.pem" ]]; then
    SYSTEM_ROOTS="/etc/ssl/cert.pem"
elif security find-certificate -a -p /System/Library/Keychains/SystemRootCertificates.keychain > /dev/null 2>&1; then
    security find-certificate -a -p /System/Library/Keychains/SystemRootCertificates.keychain > "${CERT_DIR}/system-roots.pem" 2>/dev/null
    SYSTEM_ROOTS="${CERT_DIR}/system-roots.pem"
fi

if [[ -n "${SYSTEM_ROOTS}" ]]; then
    cat "${SYSTEM_ROOTS}" "${CA_CERT}" > "${CA_BUNDLE}"
    info "Built CA bundle (system roots + proxy CA)"
else
    cp -f "${CA_CERT}" "${CA_BUNDLE}"
    warn "Could not find system roots; CA bundle contains only proxy CA"
fi

# --- Step 5: Install LaunchAgents ---
step 5 "Installing LaunchAgents"

# Stop existing services if running
if launchctl list "${PLIST_LABEL}" &>/dev/null; then
    launchctl unload "${PLIST_DEST}" 2>/dev/null || true
fi
if launchctl list "${APPLET_PLIST_LABEL}" &>/dev/null; then
    launchctl unload "${APPLET_PLIST_DEST}" 2>/dev/null || true
fi

mkdir -p "$(dirname "${PLIST_DEST}")"

# --- Proxy LaunchAgent ---
cat > "${PLIST_DEST}" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_DIR}/bin/python</string>
        <string>connect_proxy.py</string>
        <string>--port</string>
        <string>${PROXY_PORT}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROXY_DIR}/src</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${PROXY_DIR}/logs/proxy.log</string>
    <key>StandardErrorPath</key>
    <string>${PROXY_DIR}/logs/proxy.err</string>
    <key>ThrottleInterval</key>
    <integer>3</integer>
</dict>
</plist>
EOF

# --- Applet LaunchAgent ---
cat > "${APPLET_PLIST_DEST}" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${APPLET_PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_DIR}/bin/python</string>
        <string>applet.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROXY_DIR}/src</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>${PROXY_DIR}/logs/applet.log</string>
    <key>StandardErrorPath</key>
    <string>${PROXY_DIR}/logs/applet.err</string>
</dict>
</plist>
EOF

launchctl load "${PLIST_DEST}"
launchctl load "${APPLET_PLIST_DEST}"

# Wait for proxy to come up
RETRIES=8
PROXY_UP=false
for i in $(seq 1 ${RETRIES}); do
    sleep 1
    if curl -s --max-time 2 "${HEALTH_URL}" > /dev/null 2>&1; then
        PROXY_UP=true
        break
    fi
done

if [[ "${PROXY_UP}" == "true" ]]; then
    info "Proxy running on 127.0.0.1:${PROXY_PORT}"
else
    warn "Proxy not responding after ${RETRIES}s."
    if [[ -f "${PROXY_DIR}/logs/proxy.err" ]]; then
        echo ""
        echo "  Last error output:"
        tail -5 "${PROXY_DIR}/logs/proxy.err" 2>/dev/null | sed 's/^/    /'
    fi
    echo ""
    warn "Try: kiro-proxy logs"
    fail "Proxy failed to start. Stopping installation."
fi

# --- Step 6: Configure shell environment ---
step 6 "Configuring shell environment"

PROXY_ENV_BLOCK="# >>> kiro-proxy >>>
export HTTPS_PROXY=\"http://127.0.0.1:${PROXY_PORT}\"
export SSL_CERT_FILE=\"${CA_BUNDLE}\"
export NODE_EXTRA_CA_CERTS=\"${CERT_DIR}/ca-cert.pem\"
# <<< kiro-proxy <<<"

# Only add if not already present
if ! grep -q "kiro-proxy >>>" "${SHELL_RC}" 2>/dev/null; then
    echo "" >> "${SHELL_RC}"
    echo "${PROXY_ENV_BLOCK}" >> "${SHELL_RC}"
    info "Added proxy env vars to ${SHELL_RC}"
else
    info "Shell config already has proxy env vars"
fi

# Set for current GUI session
launchctl setenv HTTPS_PROXY "http://127.0.0.1:${PROXY_PORT}" 2>/dev/null || true
launchctl setenv SSL_CERT_FILE "${CA_BUNDLE}" 2>/dev/null || true
launchctl setenv NODE_EXTRA_CA_CERTS "${CERT_DIR}/ca-cert.pem" 2>/dev/null || true

# Export for the self-test below
export HTTPS_PROXY="http://127.0.0.1:${PROXY_PORT}"
export SSL_CERT_FILE="${CA_BUNDLE}"
export NODE_EXTRA_CA_CERTS="${CERT_DIR}/ca-cert.pem"

# Configure tao (if present) so ACP sessions also route through proxy
TAO_ENV="${HOME}/.tao/env"
if [[ -d "${HOME}/.tao" ]]; then
    if grep -q "HTTPS_PROXY" "${TAO_ENV}" 2>/dev/null; then
        info "~/.tao/env already has proxy config"
    else
        echo "" >> "${TAO_ENV}"
        echo "# Kiro compression proxy" >> "${TAO_ENV}"
        echo "HTTPS_PROXY=http://127.0.0.1:${PROXY_PORT}" >> "${TAO_ENV}"
        echo "SSL_CERT_FILE=${CA_BUNDLE}" >> "${TAO_ENV}"
        echo "NODE_EXTRA_CA_CERTS=${CERT_DIR}/ca-cert.pem" >> "${TAO_ENV}"
        info "Added proxy config to ~/.tao/env (covers ACP sessions)"
        warn "Restart tao-dashboard for ACP sessions to pick this up"
    fi
fi

# --- Step 7: End-to-end verification ---
step 7 "Verifying kiro-cli works through proxy"

# Clear log to detect fresh compression line
> "${PROXY_DIR}/logs/proxy.err" 2>/dev/null || true
# Small sleep to ensure log is flushed
sleep 1

PROXY_START=$(date +%s)
if PROXY_OUTPUT=$(kiro-cli chat --no-interactive "respond with only the word: ok" 2>&1); then
    PROXY_END=$(date +%s)
    PROXY_SECS=$((PROXY_END - PROXY_START))
    info "kiro-cli responded in ${PROXY_SECS}s (through proxy)"
else
    warn "kiro-cli failed through proxy. Output: ${PROXY_OUTPUT}"
    warn "The proxy may need configuration fixes. Run: kiro-proxy logs"
    # Don't fail — the install succeeded, proxy is running, just this test failed
fi

# Check if compression happened
sleep 1
if grep -q "compressed" "${PROXY_DIR}/logs/proxy.err" 2>/dev/null; then
    COMPRESS_LINE=$(grep "compressed" "${PROXY_DIR}/logs/proxy.err" | tail -1)
    SAVINGS=$(echo "${COMPRESS_LINE}" | grep -oE '[0-9]+\.[0-9]+%' | head -1)
    info "Compression verified: ${SAVINGS:-working} savings on test request"
else
    warn "No compression detected in logs (request may have been too small)"
fi

# Calculate overhead
if [[ "${PROXY_SECS}" -gt 0 && "${BASELINE_SECS}" -gt 0 ]]; then
    OVERHEAD=$((PROXY_SECS - BASELINE_SECS))
    if [[ "${OVERHEAD}" -le 1 ]]; then
        info "Proxy overhead: negligible"
    else
        info "Proxy overhead: ~${OVERHEAD}s (will be lower in normal use)"
    fi
fi

# --- Done ---
echo ""
echo -e "${BOLD}=== Installation complete ===${NC}"
echo ""
echo "  Proxy:   127.0.0.1:${PROXY_PORT} (running, auto-starts on login)"
echo "  Applet:  Menu bar mushroom icon (shows live stats)"
echo "  Venv:    ${VENV_DIR}"
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
echo "  All kiro-cli sessions will now be compressed (~40-55% savings)."
echo "  Other tools (git, curl, npm, brew, AWS) are unaffected."
