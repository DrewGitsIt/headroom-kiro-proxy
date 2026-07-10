#!/bin/bash
# kiro-wrapper.sh — Runs kiro-cli through the compression proxy if available.
#
# If the proxy is healthy: routes through proxy with SSL_CERT_FILE + HTTPS_PROXY
# If the proxy is down:    runs kiro-cli directly (no compression, no failure)
#
# This script is aliased as `kiro-cli` in the user's shell profile.

PROXY_DIR="${HOME}/.kiro-proxy"
PROXY_PORT=9090
HEALTH_URL="http://127.0.0.1:${PROXY_PORT}/health"
CA_BUNDLE="${PROXY_DIR}/ca-bundle.pem"

# Find the real kiro-cli binary (not this wrapper)
KIRO_CLI="$(command -v kiro-cli 2>/dev/null)"
if [[ -z "${KIRO_CLI}" ]]; then
    # Fallback: common install locations
    for candidate in "${HOME}/.local/bin/kiro-cli" "/usr/local/bin/kiro-cli"; do
        if [[ -x "${candidate}" ]]; then
            KIRO_CLI="${candidate}"
            break
        fi
    done
fi

if [[ -z "${KIRO_CLI}" ]]; then
    echo "Error: kiro-cli not found" >&2
    exit 1
fi

# Quick health check (200ms timeout — adds negligible startup latency)
if [[ -f "${CA_BUNDLE}" ]] && curl -s --max-time 0.2 "${HEALTH_URL}" > /dev/null 2>&1; then
    # Proxy is up — route through it
    exec env SSL_CERT_FILE="${CA_BUNDLE}" \
             HTTPS_PROXY="http://127.0.0.1:${PROXY_PORT}" \
             "${KIRO_CLI}" "$@"
else
    # Proxy is down — run direct (uncompressed but functional)
    exec "${KIRO_CLI}" "$@"
fi
