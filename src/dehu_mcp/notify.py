"""Deliver un-notified events: desktop notification (macOS / Linux notify-send) + optional ntfy.

Phone pushes: set <PREFIX>_NTFY_URL=https://ntfy.sh/<long-random-topic> (or your own ntfy server).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import requests

from . import APP_NAME, ENV_PREFIX
from .store import Store

URGENT = {
    "dehu_pending_notification", "aeat_debt_executive", "aeat_plazo_overdue", "sync_failing", "cert_expiring",
}


def _desktop(title: str, body: str) -> None:
    if sys.platform == "darwin":
        def q(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')[:240]
        subprocess.run(["osascript", "-e", f'display notification "{q(body)}" with title "{q(title)}" sound name "Glass"'],
                       capture_output=True)
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", title, body[:240]], capture_output=True)


def _ntfy(title: str, body: str, urgent: bool) -> None:
    url = os.environ.get(f"{ENV_PREFIX}_NTFY_URL")
    if not url:
        return
    try:
        requests.post(url, data=body.encode(), timeout=15, headers={
            "Title": title.encode("utf-8").decode("latin-1", "ignore"),
            "Priority": "high" if urgent else "default"})
    except requests.RequestException:
        pass


def deliver(store: Store, profile_name: str, max_individual: int = 5) -> int:
    evs = store.unnotified()
    if not evs:
        return 0
    for e in evs[:max_individual]:
        title = f"{'⚠️ ' if e['type'] in URGENT else ''}{APP_NAME} · {profile_name}"
        _desktop(title, e["summary"])
        _ntfy(title, e["summary"], e["type"] in URGENT)
    if len(evs) > max_individual:
        rest = len(evs) - max_individual
        _desktop(f"{APP_NAME} · {profile_name}", f"+{rest} more changes (ask your assistant: 'what changed?')")
        _ntfy(f"{APP_NAME} · {profile_name}", f"+{rest} more changes", False)
    store.mark_notified([e["id"] for e in evs])
    store.commit()
    return len(evs)
