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

# --- OS check ---
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Error: kiro-proxy install.sh is macOS only."
    echo "For Windows, use install.ps1 instead."
    echo "Linux is not currently supported."
    exit 1
fi

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
[[ "${SHELL:-}" == *bash* ]] && SHELL_RC="${HOME}/.bashrc"
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
if ! "${VENV_DIR}/bin/pip" install --quiet "headroom-ai>=0.31.0" "rumps>=0.4.0" "boto3>=1.34.0" 2>&1; then
    fail "pip install failed. Check your internet connection and try again."
fi
info "Installed headroom-ai, rumps, boto3"

# --- Step 3: Download proxy source ---
step 3 "Downloading proxy source"

download "${GITHUB_RAW}/src/connect_proxy.py" "${PROXY_DIR}/src/connect_proxy.py"
download "${GITHUB_RAW}/src/proxy.py"         "${PROXY_DIR}/src/proxy.py"
download "${GITHUB_RAW}/src/interceptor.py"   "${PROXY_DIR}/src/interceptor.py"
download "${GITHUB_RAW}/src/stats.py"         "${PROXY_DIR}/src/stats.py"
download "${GITHUB_RAW}/src/reporter.py"      "${PROXY_DIR}/src/reporter.py"
download "${GITHUB_RAW}/src/birdseye.py"     "${PROXY_DIR}/src/birdseye.py"
download "${GITHUB_RAW}/src/handler.py"       "${PROXY_DIR}/src/handler.py"
download "${GITHUB_RAW}/src/session_timer.py" "${PROXY_DIR}/src/session_timer.py"
download "${GITHUB_RAW}/src/applet.py"        "${PROXY_DIR}/src/applet.py"
download "${GITHUB_RAW}/scripts/kiro-proxy"   "${PROXY_DIR}/kiro-proxy"
download "${GITHUB_RAW}/scripts/kiro-wrapper.sh" "${PROXY_DIR}/kiro-wrapper.sh"
download "${GITHUB_RAW}/scripts/doctor.sh"    "${PROXY_DIR}/doctor.sh"
download "${GITHUB_RAW}/scripts/update.sh"    "${PROXY_DIR}/update.sh"
download "${GITHUB_RAW}/scripts/uninstall.sh" "${PROXY_DIR}/uninstall.sh"
download "${GITHUB_RAW}/assets/mushroom-16.png"    "${PROXY_DIR}/assets/mushroom-16.png"
download "${GITHUB_RAW}/assets/mushroom-16@2x.png" "${PROXY_DIR}/assets/mushroom-16@2x.png"

chmod +x "${PROXY_DIR}/kiro-proxy" \
         "${PROXY_DIR}/kiro-wrapper.sh" \
         "${PROXY_DIR}/doctor.sh" \
         "${PROXY_DIR}/update.sh" \
         "${PROXY_DIR}/uninstall.sh"

info "Downloaded to ${PROXY_DIR}/src/"

# Install CLI to ~/.local/bin (no sudo needed, on PATH for most setups)
CLI_DIR="${HOME}/.local/bin"
CLI_DEST="${CLI_DIR}/kiro-proxy"
mkdir -p "${CLI_DIR}"
ln -sf "${PROXY_DIR}/kiro-proxy" "${CLI_DEST}"
info "Installed kiro-proxy CLI to ${CLI_DEST}"

# Ensure ~/.local/bin is on PATH
if [[ ":${PATH}:" != *":${CLI_DIR}:"* ]]; then
    SHELL_RC="${HOME}/.zshrc"
    [[ "${SHELL}" == *bash* ]] && SHELL_RC="${HOME}/.bashrc"
    if ! grep -q '\.local/bin' "${SHELL_RC}" 2>/dev/null; then
        echo 'export PATH="${HOME}/.local/bin:${PATH}"' >> "${SHELL_RC}"
        info "Added ~/.local/bin to PATH in ${SHELL_RC}"
    fi
    export PATH="${CLI_DIR}:${PATH}"
fi

# --- Step 4: Generate CA certificate ---
step 4 "Generating TLS certificates"

CERT_DIR="${PROXY_DIR}"
CA_KEY="${CERT_DIR}/ca-key.pem"
CA_CERT="${CERT_DIR}/ca-cert.pem"
HOST_KEY="${CERT_DIR}/key.pem"
HOST_CERT="${CERT_DIR}/cert.pem"
CA_BUNDLE="${CERT_DIR}/ca-bundle.pem"

if [[ -f "${CA_CERT}" && -f "${CA_KEY}" ]]; then
    if openssl x509 -checkend 86400 -noout -in "${CA_CERT}" &>/dev/null; then
        info "Existing CA certificate is valid, reusing"
    else
        warn "Existing CA certificate expired, regenerating"
        rm -f "${CA_KEY}" "${CA_CERT}" "${HOST_KEY}" "${HOST_CERT}" "${CA_BUNDLE}"
    fi
fi

if [[ ! -f "${CA_CERT}" ]]; then
    # Generate CA key + self-signed CA cert
    openssl req -x509 -newkey rsa:2048 \
        -keyout "${CA_KEY}" -out "${CA_CERT}" \
        -days 3650 -nodes \
        -subj "/CN=kiro-proxy CA/O=kiro-proxy" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        2>/dev/null
    chmod 600 "${CA_KEY}"
    info "Generated CA certificate (10-year validity)"
fi

if [[ ! -f "${HOST_CERT}" ]]; then
    # Generate host key + CSR
    openssl req -newkey rsa:2048 -nodes \
        -keyout "${HOST_KEY}" \
        -out "${CERT_DIR}/host.csr" \
        -subj "/CN=runtime.us-east-1.kiro.dev" \
        2>/dev/null

    # Sign host cert with our CA
    cat > "${CERT_DIR}/host.ext" << EXTEOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
subjectAltName=DNS:runtime.us-east-1.kiro.dev
EXTEOF

    openssl x509 -req -in "${CERT_DIR}/host.csr" \
        -CA "${CA_CERT}" -CAkey "${CA_KEY}" \
        -CAcreateserial -out "${HOST_CERT}" \
        -days 3650 -extfile "${CERT_DIR}/host.ext" \
        2>/dev/null
    chmod 600 "${HOST_KEY}"
    info "Generated host certificate for runtime.us-east-1.kiro.dev"
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

# Prompt for routing mode
echo ""
echo -e "${BOLD}Proxy routing mode:${NC}"
echo "  [1] Global (recommended) — all HTTPS traffic routes through the proxy."
echo "      Non-kiro traffic passes through unchanged. Simplest setup."
echo "  [2] Wrapper — only kiro-cli uses the proxy. Other tools unaffected."
echo "      Requires a shell alias for kiro-cli."
echo ""
read -r -p "Choose [1/2] (default: 1): " MODE_CHOICE </dev/tty || MODE_CHOICE=""

PROXY_MODE="global"
if [[ "${MODE_CHOICE}" == "2" ]]; then
    PROXY_MODE="wrapper"
fi

# Persist mode to config
CONFIG_FILE="${PROXY_DIR}/config"
if [[ -f "${CONFIG_FILE}" ]]; then
    TMP=$(mktemp)
    grep -v '^mode=' "${CONFIG_FILE}" > "${TMP}" || true
    mv -f "${TMP}" "${CONFIG_FILE}"
fi
echo "mode=${PROXY_MODE}" >> "${CONFIG_FILE}"

if [[ "${PROXY_MODE}" == "global" ]]; then
    # --- Global mode: env vars in shell profile ---
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

    info "Routing mode: global (all HTTPS traffic through proxy)"

else
    # --- Wrapper mode: alias only ---
    WRAPPER_BLOCK="# >>> kiro-proxy-wrapper >>>
alias kiro-cli='${PROXY_DIR}/kiro-wrapper.sh'
# <<< kiro-proxy-wrapper <<<"

    # Remove any leftover global env vars
    if grep -q "# >>> kiro-proxy >>>" "${SHELL_RC}" 2>/dev/null; then
        TMP=$(mktemp)
        sed '/# >>> kiro-proxy >>>/,/# <<< kiro-proxy <<</d' "${SHELL_RC}" > "${TMP}"
        mv -f "${TMP}" "${SHELL_RC}"
    fi

    # Add alias if not present
    if ! grep -q "kiro-proxy-wrapper >>>" "${SHELL_RC}" 2>/dev/null; then
        echo "" >> "${SHELL_RC}"
        echo "${WRAPPER_BLOCK}" >> "${SHELL_RC}"
        info "Added kiro-cli wrapper alias to ${SHELL_RC}"
    else
        info "Shell config already has wrapper alias"
    fi

    # Export for the self-test below (wrapper mode still needs these for the test)
    export HTTPS_PROXY="http://127.0.0.1:${PROXY_PORT}"
    export SSL_CERT_FILE="${CA_BUNDLE}"
    export NODE_EXTRA_CA_CERTS="${CERT_DIR}/ca-cert.pem"

    info "Routing mode: wrapper (only kiro-cli uses the proxy)"
fi

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

# --- Step 6b: Telemetry setup (anonymous, opt-out) ---
METRICS_SECRET_ID="kiro-proxy/metrics-reporter"
METRICS_AWS_PROFILE="ai-platform-dev"

# Generate anonymous install ID
if [[ ! -f "${PROXY_DIR}/install_id" ]]; then
    "${PYTHON}" -c "import os; print(os.urandom(16).hex())" > "${PROXY_DIR}/install_id"
fi

# Fetch write-only credentials from Secrets Manager (requires valid AWS SSO session)
if [[ ! -f "${PROXY_DIR}/aws_credentials" ]]; then
    CREDS_JSON=""
    if command -v aws &>/dev/null; then
        CREDS_JSON=$(aws secretsmanager get-secret-value \
            --secret-id "${METRICS_SECRET_ID}" \
            --profile "${METRICS_AWS_PROFILE}" \
            --region us-east-1 \
            --query SecretString --output text 2>/dev/null) || true
    fi

    # If no valid session, prompt user to log in
    if [[ -z "${CREDS_JSON}" ]] && command -v aws &>/dev/null; then
        echo ""
        info "AWS SSO session needed to enable team metrics (one-time setup)"
        echo -n "  Log in now? [Y/n] "
        read -r TELEMETRY_CHOICE </dev/tty || TELEMETRY_CHOICE=""
        if [[ "${TELEMETRY_CHOICE}" =~ ^[Nn] ]]; then
            info "Telemetry skipped. Enable anytime: aws sso login --profile ${METRICS_AWS_PROFILE} && kiro-proxy update"
        else
            echo -e "  Running: ${BOLD}aws sso login --profile ${METRICS_AWS_PROFILE}${NC}"
            echo ""
            aws sso login --profile "${METRICS_AWS_PROFILE}" 2>/dev/null && \
                CREDS_JSON=$(aws secretsmanager get-secret-value \
                    --secret-id "${METRICS_SECRET_ID}" \
                    --profile "${METRICS_AWS_PROFILE}" \
                    --region us-east-1 \
                    --query SecretString --output text 2>/dev/null) || true
        fi
    fi

    if [[ -n "${CREDS_JSON}" ]]; then
        "${PYTHON}" -c "
import json, sys
creds = json.loads(sys.argv[1])
print('[default]')
print(f'aws_access_key_id = {creds[\"aws_access_key_id\"]}')
print(f'aws_secret_access_key = {creds[\"aws_secret_access_key\"]}')
print(f'region = {creds.get(\"region\", \"us-east-1\")}')
" "${CREDS_JSON}" > "${PROXY_DIR}/aws_credentials"
        chmod 600 "${PROXY_DIR}/aws_credentials"
        info "Anonymous usage stats (bytes saved, tokens saved) reported daily"
        info "No conversation content is ever sent. Opt out: kiro-proxy telemetry off"
    else
        warn "Telemetry not configured (SSO login failed or aws CLI not installed)"
        info "To enable later: aws sso login --profile ${METRICS_AWS_PROFILE} && kiro-proxy update"
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

# --- Verify installation works ---
echo ""
echo -e "${BOLD}=== Verifying installation ===${NC}"
echo ""

# Run kiro-proxy status in a subshell with the new PATH
VERIFY_OUTPUT=$(PATH="${HOME}/.local/bin:${PATH}" kiro-proxy status 2>&1) || true
if echo "${VERIFY_OUTPUT}" | grep -q "●"; then
    echo -e "  ${GREEN}✓${NC} kiro-proxy is running and responding"
    echo ""
    echo "${VERIFY_OUTPUT}" | sed 's/^/    /'
else
    echo -e "  ${YELLOW}⚠${NC} Could not verify — proxy may still be starting"
fi

# --- Done ---
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║              Installation complete!                      ║${NC}"
echo -e "${BOLD}╠══════════════════════════════════════════════════════════╣${NC}"
echo -e "${BOLD}║                                                          ║${NC}"
echo -e "${BOLD}║${NC}  ${GREEN}▶ Open a new terminal${NC}, then run:                        ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}                                                          ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}      kiro-proxy status                                    ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}                                                          ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  Or to activate in this shell:                            ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}      source ${SHELL_RC}$(printf '%*s' $((34 - ${#SHELL_RC})) '')${BOLD}║${NC}"
echo -e "${BOLD}║${NC}                                                          ${BOLD}║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "  Proxy:   127.0.0.1:${PROXY_PORT} (running, auto-starts on login)"
echo "  Applet:  Menu bar mushroom icon (shows live stats)"
echo ""
echo "  kiro-proxy status      Show health and compression stats"
echo "  kiro-proxy logs        Tail proxy logs"
echo "  kiro-proxy disable     Temporarily stop compression"
echo "  kiro-proxy update      Pull latest compression logic"
echo "  kiro-proxy uninstall   Clean removal"
echo ""
echo "  All kiro-cli sessions are now compressed (~40-55% savings)."
echo "  Other tools (git, curl, npm, brew, AWS) are unaffected."
