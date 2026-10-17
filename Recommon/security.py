"""TLS contexts and certificate pinning.

Verification code handling lives in ``Recommon.pairing``; partner bookkeeping
lives in ``viewer.history``.  All that is left here is the transport identity:
a self-signed certificate per install, plus fingerprint pinning so a swapped
certificate is noticed rather than silently trusted.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import ssl
from pathlib import Path

from .config import data_dir, local_ips


# --------------------------------------------------------------------------
# certificate
# --------------------------------------------------------------------------


def _ensure_cert(role: str) -> tuple[Path, Path]:
    """Create (once) a self-signed ECDSA certificate for this install."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    base = data_dir(role)
    cert_path, key_path = base / "cert.pem", base / "key.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "lan-remote")])
    san = [x509.IPAddress(ipaddress.ip_address(ip)) for ip in local_ips()]
    san.append(x509.DNSName("localhost"))
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1))
        .not_valid_after(_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def agent_tls_context(role: str = "agent") -> ssl.SSLContext:
    cert, key = _ensure_cert(role)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    return ctx


def viewer_tls_context() -> ssl.SSLContext:
    """Verification is done manually against the pinned fingerprint."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def peer_certificate_fingerprint(ssock: ssl.SSLSocket) -> str:
    der = ssock.getpeercert(binary_form=True)
    if not der:
        raise ConnectionError("peer did not present a certificate")
    return hashlib.sha256(der).hexdigest()
