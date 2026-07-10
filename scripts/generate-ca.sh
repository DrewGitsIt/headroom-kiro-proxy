#!/bin/bash
# scripts/generate-ca.sh — Generate CA cert and combined bundle for kiro-proxy.
#
# Creates:
#   ~/.kiro-proxy/ca.key         — CA private key (chmod 600)
#   ~/.kiro-proxy/ca.pem         — CA certificate (10-year validity)
#   ~/.kiro-proxy/system-roots.pem — Current macOS system root CAs
#   ~/.kiro-proxy/ca-bundle.pem  — Combined bundle (system roots + proxy CA)
#
# The combined bundle is used as SSL_CERT_FILE for kiro-cli, scoping
# CA trust to that single process. No system keychain modification needed.

set -euo pipefail

PROXY_DIR="${HOME}/.kiro-proxy"
CA_KEY="${PROXY_DIR}/ca.key"
CA_PEM="${PROXY_DIR}/ca.pem"
SYSTEM_ROOTS="${PROXY_DIR}/system-roots.pem"
CA_BUNDLE="${PROXY_DIR}/ca-bundle.pem"

mkdir -p "${PROXY_DIR}"

# --- Generate CA key and cert (if not already present) ---
if [[ -f "${CA_KEY}" && -f "${CA_PEM}" ]]; then
    echo "CA already exists at ${CA_PEM}, skipping generation."
else
    echo "Generating CA key and certificate..."
    openssl req -x509 -newkey rsa:2048 \
        -keyout "${CA_KEY}" \
        -out "${CA_PEM}" \
        -days 3650 \
        -nodes \
        -subj "/CN=Kiro Compression Proxy CA/O=kiro-proxy/OU=local"
    chmod 600 "${CA_KEY}"
    echo "  Created: ${CA_KEY} (private key, mode 600)"
    echo "  Created: ${CA_PEM} (CA certificate, 10-year validity)"
fi

# --- Create combined key+cert for mitmproxy (expects mitmproxy-ca.pem in confdir) ---
MITM_CA="${PROXY_DIR}/mitmproxy-ca.pem"
cat "${CA_KEY}" "${CA_PEM}" > "${MITM_CA}"
chmod 600 "${MITM_CA}"
# Remove any stale auto-generated mitmproxy artifacts
rm -f "${PROXY_DIR}/mitmproxy-ca-cert.pem" "${PROXY_DIR}/mitmproxy-ca-cert.cer" \
      "${PROXY_DIR}/mitmproxy-ca.p12" "${PROXY_DIR}/mitmproxy-ca-cert.p12" \
      "${PROXY_DIR}/mitmproxy-dhparam.pem"

# --- Export system root CAs and build combined bundle ---
echo "Exporting macOS system root certificates..."
security export -t certs -f pemseq \
    -k /System/Library/Keychains/SystemRootCertificates.keychain \
    -o "${SYSTEM_ROOTS}" 2>/dev/null

echo "Building combined CA bundle..."
cat "${SYSTEM_ROOTS}" "${CA_PEM}" > "${CA_BUNDLE}"

echo "  Created: ${CA_BUNDLE} ($(wc -l < "${CA_BUNDLE}" | tr -d ' ') lines)"
echo ""
echo "Done. kiro-cli will trust this CA via:"
echo "  SSL_CERT_FILE=${CA_BUNDLE}"
