"""Command line: setup a certificate, sync, status, background job, and `serve` (the MCP server)."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import APP_NAME, ENV_PREFIX, client, config, runner
from . import cert as certmod

LABEL = f"io.github.{APP_NAME}.sync"


def cmd_setup(a) -> None:
    src = Path(a.p12).expanduser()
    if not src.exists():
        sys.exit(f"Not found: {src}")
    pw = sys.stdin.readline().rstrip("\n") if a.password_stdin else getpass.getpass(f"Password for {src.name}: ")
    try:
        ci = certmod.info_from(src, pw)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Could not open the certificate with that password: {e}")
    nif = a.nif or ci.nif
    if not nif:
        sys.exit("Could not read the NIF from the certificate; pass --nif")
    config.set_password(a.profile, pw)
    dst = config.import_p12(a.profile, src)
    prof = config.Profile(name=a.profile, nif=nif, p12_path=str(dst),
                          docs_dir=str(Path(a.docs_dir).expanduser()) if a.docs_dir else str(config.DEFAULT_DOCS))
    config.save_profile(prof, make_default=True)
    print(f"Profile '{prof.name}' saved as default (NIF {nif}).")
    print(f"Certificate valid until {ci.not_after:%Y-%m-%d} ({ci.days_left} days). Password stored in the OS keyring.")
    print(f"Documents will be saved under {prof.docs_dir}")
    print(f"Next: {APP_NAME} sync --no-notify   (first import), then {APP_NAME} install (background sync).")


def cmd_sync(a) -> None:
    prof = config.get_profile(a.profile)
    res = runner.run(prof, notify_user=not a.no_notify, **client.sync_kwargs(a))
    if a.no_notify:  # e.g. first import: don't replay these as alerts later
        st = runner.open_store(prof)
        st.mark_notified([e["id"] for e in st.unnotified()])
        st.commit()
        st.close()
    print(json.dumps(res, indent=1, ensure_ascii=False))
    if not res.get("ok"):
        sys.exit(1)


def cmd_status(a) -> None:
    prof = config.get_profile(a.profile)
    ci = certmod.info(prof)
    st = runner.open_store(prof)
    print(f"Profile {prof.name} · NIF {prof.nif} · certificate valid until {ci.not_after:%Y-%m-%d} ({ci.days_left} d)")
    for r in st.last_runs(limit=6):
        err = (r["error"] or "").splitlines()[0] if r["error"] else ""
        print(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['started']))} {'OK ' if r['ok'] else 'ERR'} {r['stats'] or ''} {err}")
    print("Recent events:")
    for e in st.events(limit=15):
        print(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(e['ts']))} [{e['type']}] {e['summary']}")


def cmd_mark_seen(a) -> None:
    prof = config.get_profile(a.profile)
    st = runner.open_store(prof)
    ids = [e["id"] for e in st.unnotified()]
    st.mark_notified(ids)
    st.commit()
    print(f"Marked {len(ids)} events as already notified.")


def _sync_command(profile: str | None) -> list[str]:
    exe = shutil.which(APP_NAME)
    if exe and ".cache/uv" not in exe and "archive-v0" not in exe:
        return [exe, "sync"] + (["--profile", profile] if profile else [])
    uvx = shutil.which("uvx") or "uvx"
    src = os.environ.get(f"{ENV_PREFIX}_SOURCE", f"git+https://github.com/isdelrey/{APP_NAME}")
    return [uvx, "--from", src, APP_NAME, "sync"] + (["--profile", profile] if profile else [])


def cmd_install(a) -> None:
    cmd = _sync_command(a.profile)
    if sys.platform != "darwin":
        print("Automatic install is macOS-only (launchd). Add this to your crontab (crontab -e):")
        print(f"0 */{int(a.hours)} * * * {' '.join(cmd)} >> {config.APP_DIR / 'sync.log'} 2>&1")
        return
    log = config.APP_DIR / "sync.log"
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + str(Path.home() / ".local" / "bin")}
    if os.environ.get(f"{ENV_PREFIX}_NTFY_URL"):
        env[f"{ENV_PREFIX}_NTFY_URL"] = os.environ[f"{ENV_PREFIX}_NTFY_URL"]
    plist = {"Label": LABEL, "ProgramArguments": cmd, "StartInterval": int(a.hours * 3600), "RunAtLoad": True,
             "StandardOutPath": str(log), "StandardErrorPath": str(log), "EnvironmentVariables": env,
             "ProcessType": "Background"}
    path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)
    print(f"Installed {path}: syncs every {a.hours} h. Log: {log}")


def cmd_uninstall(a) -> None:
    if sys.platform != "darwin":
        print("Remove the line from your crontab (crontab -e).")
        return
    path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
    path.unlink(missing_ok=True)
    print("Background sync removed.")


def cmd_serve(a) -> None:
    from .server import mcp
    mcp.run()


def main() -> None:
    p = argparse.ArgumentParser(prog=APP_NAME)
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("setup", help="register a .p12/.pfx certificate")
    s.add_argument("--p12", required=True)
    s.add_argument("--profile", default="default")
    s.add_argument("--nif", help="override the NIF read from the certificate")
    s.add_argument("--docs-dir", help=f"where PDFs are saved (default {config.DEFAULT_DOCS})")
    s.add_argument("--password-stdin", action="store_true", help="read the password from stdin instead of prompting")
    s.set_defaults(f=cmd_setup)
    s = sub.add_parser("sync", help="log in and refresh now")
    s.add_argument("--profile")
    s.add_argument("--no-notify", action="store_true")
    client.add_sync_args(s)
    s.set_defaults(f=cmd_sync)
    s = sub.add_parser("status")
    s.add_argument("--profile")
    s.set_defaults(f=cmd_status)
    s = sub.add_parser("mark-seen", help="mark all current events as notified (after a first import)")
    s.add_argument("--profile")
    s.set_defaults(f=cmd_mark_seen)
    s = sub.add_parser("install", help="background sync (launchd on macOS; prints a cron line elsewhere)")
    s.add_argument("--hours", type=float, default=4)
    s.add_argument("--profile")
    s.set_defaults(f=cmd_install)
    s = sub.add_parser("uninstall")
    s.set_defaults(f=cmd_uninstall)
    s = sub.add_parser("serve", help="run the MCP server (stdio)")
    s.set_defaults(f=cmd_serve)
    a = p.parse_args()
    if not a.cmd:
        p.print_help()
        return
    try:
        a.f(a)
    except config.ConfigError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
