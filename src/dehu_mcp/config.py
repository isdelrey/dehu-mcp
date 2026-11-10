"""Profiles (one per certificate holder), paths and certificate password storage.

A profile = one .p12/.pfx certificate. On `setup` the file is copied into the app data dir
(mode 600) and its password is stored in the OS keyring (macOS Keychain, Windows Credential
Manager, Secret Service on Linux) — never in plain files and never passed through MCP calls.
Env overrides: <PREFIX>_HOME, <PREFIX>_PROFILE, <PREFIX>_P12_PASSWORD (for CI/headless boxes).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import APP_NAME, ENV_PREFIX, DEFAULT_DOCS_DIRNAME


def _default_home() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME


APP_DIR = Path(os.environ.get(f"{ENV_PREFIX}_HOME", _default_home()))
PROFILES_FILE = APP_DIR / "profiles.json"
DEFAULT_DOCS = Path.home() / "Documents" / DEFAULT_DOCS_DIRNAME


@dataclass
class Profile:
    name: str
    nif: str
    p12_path: str
    docs_dir: str = str(DEFAULT_DOCS)
    options: dict = field(default_factory=dict)

    @property
    def dir(self) -> Path:
        return APP_DIR / self.name

    @property
    def db_path(self) -> Path:
        return self.dir / "state.db"

    @property
    def raw_dir(self) -> Path:
        return self.dir / "raw"


class ConfigError(RuntimeError):
    pass


def _load_all() -> dict:
    if PROFILES_FILE.exists():
        return json.loads(PROFILES_FILE.read_text())
    return {"default": None, "profiles": {}}


def _save_all(d: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(APP_DIR, 0o700)
    except OSError:
        pass
    PROFILES_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    try:
        os.chmod(PROFILES_FILE, 0o600)
    except OSError:
        pass


def list_profiles() -> list[Profile]:
    return [Profile(**p) for p in _load_all()["profiles"].values()]


def get_profile(name: str | None = None) -> Profile:
    d = _load_all()
    name = name or os.environ.get(f"{ENV_PREFIX}_PROFILE") or d.get("default")
    if not name or name not in d["profiles"]:
        raise ConfigError(
            f"No certificate configured. In a terminal, run:\n"
            f"  {APP_NAME} setup --p12 /path/to/certificate.p12\n"
            f"It asks for the certificate password and stores it in the OS keyring."
        )
    return Profile(**d["profiles"][name])


def save_profile(p: Profile, make_default: bool = True) -> None:
    d = _load_all()
    d["profiles"][p.name] = asdict(p)
    if make_default or not d.get("default"):
        d["default"] = p.name
    _save_all(d)


def import_p12(name: str, src: Path) -> Path:
    dst_dir = APP_DIR / name
    dst_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dst_dir, 0o700)
    except OSError:
        pass
    dst = dst_dir / "cert.p12"
    shutil.copyfile(src, dst)
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
    return dst


def set_password(profile: str, password: str) -> None:
    import keyring

    keyring.set_password(APP_NAME, profile, password)


def get_password(profile: str) -> str:
    env = os.environ.get(f"{ENV_PREFIX}_P12_PASSWORD")
    if env:
        return env
    import keyring

    pw = keyring.get_password(APP_NAME, profile)
    if pw is None:
        raise ConfigError(f"No password for profile '{profile}' in the keyring. Run: {APP_NAME} setup --p12 <file>")
    return pw
