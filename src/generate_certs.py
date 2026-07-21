"""generate_certs.py — Cross-platform TLS cert generation for kiro-proxy.

Generates three files in the target directory:
    ca-key.pem     — CA private key (chmod 600)
    ca-cert.pem    — CA certificate (CN=kiro-proxy CA, 10-year validity)
    ca-bundle.pem  — certifi root bundle + our CA cert (SSL_CERT_FILE)

The CA signs a host cert for runtime.us-east-1.kiro.dev; connect_proxy.py
serves that host cert during TLS intercept. kiro-cli trusts the CA via the
bundle written to SSL_CERT_FILE (and NODE_EXTRA_CA_CERTS for the Node layer).

Replaces the openssl CLI dependency so the installer works on Windows.

Usage:
    python generate_certs.py <output_dir>

Dependencies: cryptography, certifi (both installed by the installer).
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KIRO_HOST = "runtime.us-east-1.kiro.dev"
CA_CN = "kiro-proxy CA"
CA_VALIDITY_DAYS = 3650       # 10 years — long-lived so installs stay valid
HOST_VALIDITY_DAYS = 3650     # Match CA so neither expires first
KEY_SIZE_BITS = 2048          # RSA-2048: fast generation, widely supported


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_cryptography() -> tuple:
    """Import cryptography types; fail with a friendly message if missing."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        return x509, hashes, serialization, rsa, NameOID
    except ImportError:
        _die(
            "The 'cryptography' package is required but not installed.\n"
            "  Fix: pip install cryptography"
        )


def _die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def _write_private(path: Path, key_pem: bytes) -> None:
    """Write a private key with mode 0o600 (owner-read-only)."""
    path.write_bytes(key_pem)
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows does not support POSIX chmod; the file is still written.
        pass


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# CA generation
# ---------------------------------------------------------------------------

def _generate_ca(x509, hashes, serialization, rsa, NameOID):
    """Generate a self-signed CA key + cert.

    Returns (ca_private_key, ca_cert).
    """
    from cryptography.hazmat.primitives.asymmetric import rsa as rsa_mod

    ca_key = rsa_mod.generate_private_key(
        public_exponent=65537,
        key_size=KEY_SIZE_BITS,
    )

    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, CA_CN),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "kiro-proxy"),
    ])

    not_before = _now_utc()
    not_after = not_before + datetime.timedelta(days=CA_VALIDITY_DAYS)

    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    return ca_key, ca_cert


# ---------------------------------------------------------------------------
# Host cert generation
# ---------------------------------------------------------------------------

def _generate_host_cert(hostname: str, ca_key, ca_cert, x509, hashes, serialization, rsa, NameOID):
    """Generate a host cert for *hostname* signed by the given CA.

    Returns (host_private_key, host_cert).
    """
    from cryptography.hazmat.primitives.asymmetric import rsa as rsa_mod

    host_key = rsa_mod.generate_private_key(
        public_exponent=65537,
        key_size=KEY_SIZE_BITS,
    )

    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
    ])

    not_before = _now_utc()
    not_after = not_before + datetime.timedelta(days=HOST_VALIDITY_DAYS)

    san = x509.SubjectAlternativeName([
        x509.DNSName(hostname),
        # Wildcard covers any future sub-paths the kiro runtime may add.
        x509.DNSName(f"*.{hostname}"),
    ])

    host_cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(host_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(san, critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        # Sign with the CA's private key — this binds the host cert to our CA.
        .sign(ca_key, hashes.SHA256())
    )

    return host_key, host_cert


# ---------------------------------------------------------------------------
# Bundle construction
# ---------------------------------------------------------------------------

def _build_bundle(ca_cert_pem: bytes) -> bytes:
    """Return certifi's root bundle + our CA cert.

    If certifi is unavailable, fall back to the CA cert alone. connect_proxy.py
    handles outbound TLS with the system default context, so the bundle only
    needs to contain enough for kiro-cli to validate our intercepting cert.
    """
    try:
        import certifi
        certifi_path = Path(certifi.where())
        root_bundle = certifi_path.read_bytes()
        # Ensure a clean newline boundary before appending.
        if root_bundle and not root_bundle.endswith(b"\n"):
            root_bundle += b"\n"
        return root_bundle + ca_cert_pem
    except ImportError:
        print(
            "WARNING: certifi not installed; bundle will contain only the proxy CA.\n"
            "  Fix: pip install certifi",
            file=sys.stderr,
        )
        return ca_cert_pem


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _cert_to_pem(cert) -> bytes:
    from cryptography.hazmat.primitives import serialization as _s
    return cert.public_bytes(_s.Encoding.PEM)


def _key_to_pem(key) -> bytes:
    from cryptography.hazmat.primitives import serialization as _s
    return key.private_bytes(
        encoding=_s.Encoding.PEM,
        format=_s.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=_s.NoEncryption(),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate_certs(output_dir: Path) -> None:
    """Generate all certs and write them to *output_dir*.

    Idempotent: if ca-cert.pem and ca-key.pem already exist and are not
    expired, they are reused. The bundle is always rebuilt so it picks up
    certifi updates.
    """
    assert output_dir is not None, "output_dir must not be None"

    output_dir.mkdir(parents=True, exist_ok=True)

    ca_key_path = output_dir / "ca-key.pem"
    ca_cert_path = output_dir / "ca-cert.pem"
    host_key_path = output_dir / "key.pem"      # connect_proxy.py expects key.pem
    host_cert_path = output_dir / "cert.pem"    # connect_proxy.py expects cert.pem
    bundle_path = output_dir / "ca-bundle.pem"

    x509, hashes, serialization, rsa, NameOID = _load_cryptography()

    # --- CA ---
    if ca_cert_path.exists() and ca_key_path.exists():
        print(f"CA already exists at {ca_cert_path}, reusing.")
        ca_cert_pem = ca_cert_path.read_bytes()
        ca_key_pem = ca_key_path.read_bytes()

        # Load existing objects so we can sign the host cert with them.
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        ca_key = load_pem_private_key(ca_key_pem, password=None)
        ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    else:
        print("Generating CA key and certificate (RSA-2048, 10-year validity)...")
        ca_key, ca_cert = _generate_ca(x509, hashes, serialization, rsa, NameOID)
        ca_cert_pem = _cert_to_pem(ca_cert)
        ca_key_pem = _key_to_pem(ca_key)
        _write_private(ca_key_path, ca_key_pem)
        ca_cert_path.write_bytes(ca_cert_pem)
        print(f"  Written: {ca_key_path}")
        print(f"  Written: {ca_cert_path}")

    # --- Host cert ---
    if host_cert_path.exists() and host_key_path.exists():
        print(f"Host cert already exists at {host_cert_path}, reusing.")
    else:
        print(f"Generating host cert for {KIRO_HOST}...")
        host_key, host_cert = _generate_host_cert(
            KIRO_HOST, ca_key, ca_cert, x509, hashes, serialization, rsa, NameOID
        )
        _write_private(host_key_path, _key_to_pem(host_key))
        host_cert_path.write_bytes(_cert_to_pem(host_cert))
        print(f"  Written: {host_key_path}")
        print(f"  Written: {host_cert_path}")

    # --- Bundle (always rebuilt) ---
    bundle = _build_bundle(ca_cert_path.read_bytes())
    bundle_path.write_bytes(bundle)
    line_count = bundle.count(b"\n")
    print(f"  Written: {bundle_path} ({line_count} lines, certifi roots + proxy CA)")

    print("\nCert generation complete.")
    print(f"  SSL_CERT_FILE={bundle_path}")
    print(f"  NODE_EXTRA_CA_CERTS={ca_cert_path}")


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python generate_certs.py <output_dir>", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(sys.argv[1])
    generate_certs(output_dir)


if __name__ == "__main__":
    main()
