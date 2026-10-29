#!/usr/bin/env python3
"""Generate the local cloud stub's own CA and server certificate.

This is self-hosting, not impersonation: we issue our own CA for a domain we
own (`*.autoflash-stub.test`, reserved by RFC 6761), make our own firmware copy trust
it (see `patch_stock.py --trust-ca`), and serve it from our own stub. coolkit's
name, cert and CA private key are never used or forged.

The root CA is **EC P-256** on purpose: its PEM (~650 bytes) fits the stock
firmware's fixed 1040-byte trust-anchor slot, which an RSA-2048 CA PEM (~1200
bytes) would overflow (see `patch_stock.py`).

Written to `captures/emulation/stub-cloud/` (Git-ignored, private):

    ca.crt / ca.key         the root CA (ca.crt feeds the firmware trust swap)
    server.crt / server.key the stub's leaf, SAN-covering the stub hosts

Safe to run before every stub start, and from several at once: the CA is made
once and kept (firmware already patched trusts it; --force makes a new one), and
the leaf is kept while it still fits - reissued only when it is missing, from
another CA, or lacks a name it now needs.

    python -I tools/espressif/emulator/stub_cloud/make_ca.py
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import ipaddress
import os
import sys
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

# The stub's private domain. `.test` is reserved (RFC 6761) and can never clash
# with a real registered name. These must match the cert SANs below, the
# /etc/hosts records and NVS `dis_server` (see docs/cloud-stub.md).
DISP_HOST = "disp.autoflash-stub.test"
IOT_HOST = "iot.autoflash-stub.test"
WILDCARD = "*.autoflash-stub.test"
# The plug connects to the IoT server dispatch hands it by numeric IP, without
# DNS (it inet_aton()s the `IP` field), and the tool's app side does too: so the
# leaf also carries the router's IP, where the stub listens.
ROUTER_IP = "192.168.60.1"

# Long-dated so the stub never needs cert renewal; the plug's clock may be
# unset at boot, so start well in the past to sidestep a not-yet-valid error.
NOT_BEFORE = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
NOT_AFTER = dt.datetime(2125, 1, 1, tzinfo=dt.timezone.utc)

# The firmware trust-anchor slot is 1040 bytes and holds a null-terminated PEM;
# the replacement CA PEM must leave room for at least one terminating null.
SLOT_BYTES = 1040


def _ec_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _write(path: Path, data: bytes, mode: int) -> None:
    """Replace `path` whole: never a half-written file, nor a key briefly
    readable before its mode is set."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _cert_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


@contextlib.contextmanager
def _locked(directory: Path):
    """One maker at a time, for emulators started side by side."""
    try:
        import fcntl
    except ImportError:  # not POSIX: the emulator runs under Linux or WSL anyway
        yield
        return
    with open(directory / ".lock", "w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def make_ca() -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = _ec_key()
    name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Autoflash cloud stub (local)"),
        x509.NameAttribute(NameOID.COMMON_NAME, "Autoflash stub local root CA"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOT_BEFORE)
        .not_valid_after(NOT_AFTER)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True,
            crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def leaf_names(router_ip: str) -> list[x509.GeneralName]:
    return [*(x509.DNSName(h) for h in (DISP_HOST, IOT_HOST, WILDCARD)),
            x509.IPAddress(ipaddress.ip_address(router_ip))]


def make_leaf(ca_key: ec.EllipticCurvePrivateKey, ca_cert: x509.Certificate, router_ip: str
              ) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = _ec_key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, DISP_HOST)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOT_BEFORE)
        .not_valid_after(NOT_AFTER)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(leaf_names(router_ip)), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def load_ca(ca_crt: Path, ca_key: Path) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    cert = x509.load_pem_x509_certificate(ca_crt.read_bytes())
    key = serialization.load_pem_private_key(ca_key.read_bytes(), password=None)
    return key, cert  # type: ignore[return-value]


def leaf_fits(crt: Path, key: Path, ca_cert: x509.Certificate, router_ip: str) -> bool:
    """Whether the leaf on disk is this CA's, matches its key, is in date and
    names every host the stub answers for."""
    try:
        cert = x509.load_pem_x509_certificate(crt.read_bytes())
        private = serialization.load_pem_private_key(key.read_bytes(), password=None)
        ca_cert.public_key().verify(cert.signature, cert.tbs_certificate_bytes,
                                    ec.ECDSA(cert.signature_hash_algorithm))
        sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (OSError, ValueError, InvalidSignature, x509.ExtensionNotFound):
        return False
    spki = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    now = dt.datetime.now(dt.timezone.utc)
    return (private.public_key().public_bytes(*spki) == cert.public_key().public_bytes(*spki)
            and cert.not_valid_before_utc <= now <= cert.not_valid_after_utc
            and all(name in sans for name in leaf_names(router_ip)))


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path,
                        default=repo / "captures" / "emulation" / "stub-cloud",
                        help="where the CA and leaf are written (Git-ignored)")
    parser.add_argument("--force", action="store_true",
                        help="make a new root CA (firmware patched to trust the old one "
                             "must then be rebuilt: emu.sh --fresh)")
    parser.add_argument("--router-ip", default=ROUTER_IP,
                        help="IP SAN for the leaf (the stub's address the plug dials)")
    args = parser.parse_args(argv)

    out = args.out_dir
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    out.chmod(0o700)
    ca_crt, ca_key = out / "ca.crt", out / "ca.key"
    srv_crt, srv_key = out / "server.crt", out / "server.key"

    with _locked(out):
        # The CA is the trust anchor baked into patched firmware, so it is kept
        # stable once made.
        if ca_crt.exists() and ca_key.exists() and not args.force:
            key, cert = load_ca(ca_crt, ca_key)
            ca_action = "kept"
        else:
            key, cert = make_ca()
            if len(_cert_pem(cert)) >= SLOT_BYTES:
                raise SystemExit(f"CA PEM is {len(_cert_pem(cert))} bytes; must be < {SLOT_BYTES} "
                                 "to fit the firmware trust slot (use an EC key, not RSA)")
            _write(ca_key, _key_pem(key), 0o600)
            _write(ca_crt, _cert_pem(cert), 0o644)
            ca_action = "made"
        # The leaf is reissued only when it no longer fits, so an ordinary start
        # rewrites nothing under a stub starting beside it (one already running
        # keeps the pair it loaded).
        if leaf_fits(srv_crt, srv_key, cert, args.router_ip):
            leaf_action = "kept"
        else:
            lkey, lcert = make_leaf(key, cert, args.router_ip)
            _write(srv_key, _key_pem(lkey), 0o600)
            _write(srv_crt, _cert_pem(lcert), 0o644)
            leaf_action = "issued"

    print(f"CA {ca_action}, leaf {leaf_action}, in {out}.")
    print(f"  CA PEM {len(ca_crt.read_bytes())} bytes (fits the {SLOT_BYTES}-byte firmware slot).")
    print(f"  Leaf SAN: {DISP_HOST}, {IOT_HOST}, {WILDCARD}, IP:{args.router_ip}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
