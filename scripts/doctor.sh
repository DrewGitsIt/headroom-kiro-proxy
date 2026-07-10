#!/bin/bash
# doctor.sh — Diagnose kiro compression proxy health.
#
# Checks: proxy running, CA valid, env vars set, kiro-cli reachable through proxy.
# Run anytime something seems wrong.
#
# Usage:
#   ~/.kiro-proxy/doctor.sh

set -uo pipefail

PROXY_DIR="${HOME}/.kiro-proxy"
PROXY_PORT=9090
HEALTH_URL="http://127.0.0.1:${PROXY_PORT}/health"
STATS_URL="http://127.0.0.1:${PROXY_PORT}/stats"
CA_BUNDLE="${PROXY_DIR}/ca-bundle.pem"
CA_PEM="${PROXY_DIR}/ca.pem"
MITM_CA="${PROXY_DIR}/mitmproxy-ca.pem"
PLIST_DEST="${HOME}/Library/LaunchAgents/com.kiro-proxy.compression.plist"
SHELL_RC="${HOME}/.zshrc"
[[ "${SHELL}" == *bash* ]] && SHELL_RC="${HOME}/.bashrc"

# Colors
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'
    BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'
else
    GREEN='' YELLOW='' RED='' BOLD='' DIM='' NC=''
fi

pass()  { echo -e "  ${GREEN}✓${NC} $*"; ((PASSED++)); }
warn()  { echo -e "  ${YELLOW}⚠${NC} $*"; ((WARNED++)); }
fail()  { echo -e "  ${RED}✗${NC} $*"; ((FAILED++)); }
hint()  { echo -e "    ${DIM}→ $*${NC}"; }

PASSED=0 WARNED=0 FAILED=0

echo -e "${BOLD}=== Kiro Proxy Doctor ===${NC}"
echo ""

# --- 1. Proxy process ---
echo -e "${BOLD}Proxy service${NC}"

if launchctl list "com.kiro-proxy.compression" &>/dev/null; then
    pass "launchd service loaded"
else
    fail "launchd service not loaded"
    hint "Run: launchctl load ${PLIST_DEST}"
fi

if pgrep -f "mitmdump.*${PROXY_PORT}" > /dev/null 2>&1; then
    PID=$(pgrep -f "mitmdump.*${PROXY_PORT}" | head -1)
    pass "mitmdump running (PID ${PID})"
else
    fail "mitmdump not running"
    hint "Check: tail -20 ${PROXY_DIR}/logs/proxy.err"
fi

if curl -s --max-time 2 "${HEALTH_URL}" > /dev/null 2>&1; then
    pass "Proxy responding on :${PROXY_PORT}"
else
    fail "Proxy not responding on :${PROXY_PORT}"
    hint "Check: tail -20 ${PROXY_DIR}/logs/proxy.err"
fi

echo ""

# --- 2. Certificates ---
echo -e "${BOLD}Certificates${NC}"

if [[ -f "${CA_PEM}" ]]; then
    EXPIRY=$(openssl x509 -in "${CA_PEM}" -noout -enddate 2>/dev/null | cut -d= -f2)
    if [[ -n "${EXPIRY}" ]]; then
        # Check if expired
        if openssl x509 -in "${CA_PEM}" -noout -checkend 0 2>/dev/null; then
            pass "CA certificate valid (expires: ${EXPIRY})"
        else
            fail "CA certificate EXPIRED (${EXPIRY})"
            hint "Delete ${PROXY_DIR}/ca.key and ca.pem, then re-run install.sh"
        fi
    else
        fail "Cannot read CA certificate"
    fi
else
    fail "CA certificate not found at ${CA_PEM}"
    hint "Re-run the installer"
fi

if [[ -f "${CA_BUNDLE}" ]]; then
    LINES=$(wc -l < "${CA_BUNDLE}" | tr -d ' ')
    if [[ ${LINES} -gt 100 ]]; then
        pass "CA bundle exists (${LINES} lines, includes system roots)"
    else
        warn "CA bundle seems too small (${LINES} lines)"
        hint "May be missing system roots. Re-run install to regenerate."
    fi
else
    fail "CA bundle not found at ${CA_BUNDLE}"
fi

if [[ -f "${MITM_CA}" ]]; then
    pass "mitmproxy CA file exists"
else
    fail "mitmproxy CA file missing (${MITM_CA})"
fi

# Check CA key permissions
if [[ -f "${PROXY_DIR}/ca.key" ]]; then
    KEY_PERMS=$(stat -f "%Lp" "${PROXY_DIR}/ca.key" 2>/dev/null)
    if [[ "${KEY_PERMS}" == "600" ]]; then
        pass "CA key permissions correct (600)"
    else
        fail "CA key permissions too open (${KEY_PERMS}, should be 600)"
        hint "Run: chmod 600 ${PROXY_DIR}/ca.key"
    fi
fi

echo ""

# --- 3. Environment variables ---
echo -e "${BOLD}Environment variables${NC}"

# Current shell
if [[ "${HTTPS_PROXY:-}" == "http://127.0.0.1:${PROXY_PORT}" ]]; then
    pass "HTTPS_PROXY set in current shell"
else
    warn "HTTPS_PROXY not set in current shell (set: '${HTTPS_PROXY:-<empty>}')"
    hint "Run: source ${SHELL_RC}"
fi

if [[ -n "${SSL_CERT_FILE:-}" && -f "${SSL_CERT_FILE:-}" ]]; then
    pass "SSL_CERT_FILE set and file exists"
else
    if [[ -z "${SSL_CERT_FILE:-}" ]]; then
        warn "SSL_CERT_FILE not set in current shell"
    else
        fail "SSL_CERT_FILE set to '${SSL_CERT_FILE}' but file not found"
    fi
    hint "Run: source ${SHELL_RC}"
fi

# Shell config
if grep -q "HTTPS_PROXY.*127.0.0.1:${PROXY_PORT}" "${SHELL_RC}" 2>/dev/null; then
    pass "HTTPS_PROXY in ${SHELL_RC}"
else
    fail "HTTPS_PROXY not found in ${SHELL_RC}"
fi

if grep -q 'SSL_CERT_FILE.*kiro-proxy' "${SHELL_RC}" 2>/dev/null; then
    # Check for the tilde bug
    if grep -q 'SSL_CERT_FILE="~/' "${SHELL_RC}" 2>/dev/null; then
        fail "SSL_CERT_FILE uses literal ~ (won't expand in quotes)"
        hint "Change to: export SSL_CERT_FILE=\"\${HOME}/.kiro-proxy/ca-bundle.pem\""
    else
        pass "SSL_CERT_FILE in ${SHELL_RC}"
    fi
else
    fail "SSL_CERT_FILE not found in ${SHELL_RC}"
fi

# launchctl env
LAUNCHCTL_PROXY=$(launchctl getenv HTTPS_PROXY 2>/dev/null || echo "")
if [[ "${LAUNCHCTL_PROXY}" == "http://127.0.0.1:${PROXY_PORT}" ]]; then
    pass "launchctl HTTPS_PROXY set (covers GUI apps)"
else
    warn "launchctl HTTPS_PROXY not set (IDE sessions may not route through proxy until reboot)"
    hint "Run: launchctl setenv HTTPS_PROXY http://127.0.0.1:${PROXY_PORT}"
fi

LAUNCHCTL_CERT=$(launchctl getenv SSL_CERT_FILE 2>/dev/null || echo "")
if [[ "${LAUNCHCTL_CERT}" == "${CA_BUNDLE}" ]]; then
    pass "launchctl SSL_CERT_FILE set"
else
    warn "launchctl SSL_CERT_FILE not set"
    hint "Run: launchctl setenv SSL_CERT_FILE ${CA_BUNDLE}"
fi

echo ""

# --- 4. tao integration ---
echo -e "${BOLD}Tao integration${NC}"

TAO_ENV="${HOME}/.tao/env"
if [[ -f "${TAO_ENV}" ]]; then
    if grep -q "HTTPS_PROXY" "${TAO_ENV}" && grep -q "SSL_CERT_FILE" "${TAO_ENV}"; then
        pass "Proxy vars in ${TAO_ENV}"
    else
        warn "Proxy vars missing from ${TAO_ENV}"
        hint "Add HTTPS_PROXY and SSL_CERT_FILE to ${TAO_ENV} for ACP coverage"
    fi
else
    pass "tao not installed (skipped)"
fi

echo ""

# --- 5. Connectivity test ---
echo -e "${BOLD}Connectivity${NC}"

if curl -s --max-time 2 "${HEALTH_URL}" > /dev/null 2>&1; then
    # Test that a CONNECT tunnel works (non-kiro host through proxy)
    TUNNEL_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
        --proxy "http://127.0.0.1:${PROXY_PORT}" \
        https://api.github.com/zen 2>/dev/null)
    if [[ "${TUNNEL_STATUS}" == "200" ]]; then
        pass "CONNECT tunnel works (GitHub API via proxy → 200)"
    else
        warn "CONNECT tunnel returned HTTP ${TUNNEL_STATUS}"
    fi

    # Test kiro endpoint (will get auth error but that's fine — proves TLS works)
    KIRO_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
        --proxy "http://127.0.0.1:${PROXY_PORT}" \
        --cacert "${CA_BUNDLE}" \
        https://runtime.us-east-1.kiro.dev/ 2>/dev/null)
    if [[ "${KIRO_STATUS}" != "000" ]]; then
        pass "Kiro endpoint reachable through proxy (HTTP ${KIRO_STATUS})"
    else
        fail "Cannot reach kiro endpoint through proxy"
        hint "Check: tail -20 ${PROXY_DIR}/logs/proxy.err"
    fi
else
    warn "Proxy not running, skipping connectivity tests"
fi

echo ""

# --- 6. Stats ---
echo -e "${BOLD}Stats${NC}"

STATS=$(curl -s --max-time 2 "${STATS_URL}" 2>/dev/null)
if [[ -n "${STATS}" ]]; then
    TOTAL=$(echo "${STATS}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('requests_total',0))" 2>/dev/null)
    COMPRESSED=$(echo "${STATS}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('requests_compressed',0))" 2>/dev/null)
    SAVINGS=$(echo "${STATS}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('savings_percent',0))" 2>/dev/null)
    pass "Lifetime: ${TOTAL} requests, ${COMPRESSED} compressed, ${SAVINGS}% savings"
fi

# --- Summary ---
echo ""
echo -e "${BOLD}Summary${NC}"
echo "  ✓ ${PASSED} passed"
[[ ${WARNED} -gt 0 ]] && echo "  ⚠ ${WARNED} warning(s)"
[[ ${FAILED} -gt 0 ]] && echo "  ✗ ${FAILED} failed"

if [[ ${FAILED} -eq 0 && ${WARNED} -eq 0 ]]; then
    echo ""
    echo -e "  ${GREEN}Everything looks good!${NC}"
elif [[ ${FAILED} -eq 0 ]]; then
    echo ""
    echo -e "  ${YELLOW}Working, but some warnings to review.${NC}"
else
    echo ""
    echo -e "  ${RED}Issues found. Check hints above.${NC}"
fi
