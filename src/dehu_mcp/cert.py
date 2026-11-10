"""Load a .p12/.pfx (including legacy RC2/3DES files, as issued by FNMT) and expose it as
short-lived PEM files: requests/Playwright need paths. They live in a fresh 0700 temp dir inside
the profile dir and are deleted when the context exits."""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, pkcs12

from .config import Profile, get_password


@dataclass
class CertInfo:
    subject: str
    serial_number: str | None
    issuer: str
    not_before: dt.datetime
    not_after: dt.datetime

    @property
    def days_left(self) -> int:
        return (self.not_after - dt.datetime.now(dt.timezone.utc)).days

    @property
    def nif(self) -> str | None:
        # FNMT puts "IDCES-<NIF>" in serialNumber
        return self.serial_number.split("-")[-1] if self.serial_number else None


def load(p12_path: str | Path, password: str):
    return pkcs12.load_key_and_certificates(Path(p12_path).read_bytes(), password.encode())


def info_from(p12_path: str | Path, password: str) -> CertInfo:
    _, cert, _ = load(p12_path, password)
    serial = next((a.value for a in cert.subject if a.oid == x509.NameOID.SERIAL_NUMBER), None)
    return CertInfo(cert.subject.rfc4514_string(), serial, cert.issuer.rfc4514_string(),
                    cert.not_valid_before_utc, cert.not_valid_after_utc)


def info(profile: Profile) -> CertInfo:
    return info_from(profile.p12_path, get_password(profile.name))


@contextlib.contextmanager
def pem_files(profile: Profile):
    """Yield (cert_path, key_path); both are removed afterwards."""
    key, cert, extra = load(profile.p12_path, get_password(profile.name))
    profile.dir.mkdir(parents=True, exist_ok=True)
    d = tempfile.mkdtemp(prefix="pem-", dir=profile.dir)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    cp, kp = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
    try:
        with open(cp, "wb") as f:
            f.write(cert.public_bytes(Encoding.PEM))
            for c in extra or []:
                f.write(c.public_bytes(Encoding.PEM))
        fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        yield cp, kp
    finally:
        shutil.rmtree(d, ignore_errors=True)
