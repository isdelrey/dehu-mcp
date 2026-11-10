"""Run a sync with bookkeeping: run log, failure alerts, certificate-expiry warnings, notifications."""
from __future__ import annotations

import time
import traceback

from . import SOURCE, cert, client, notify
from .config import Profile
from .store import Store

FAIL_ALERT_AFTER_H = 24


def open_store(profile: Profile) -> Store:
    return Store(profile.db_path)


def run(profile: Profile, notify_user: bool = True, **kw) -> dict:
    store = open_store(profile)
    try:
        try:
            ci = cert.info(profile)
            if ci.days_left <= 60 and time.time() - store.kv_get("cert:last_warn", 0) > 86400:
                store.event("system", "cert_expiring", f"Certificate '{profile.name}' expires in {ci.days_left} days "
                            f"({ci.not_after:%Y-%m-%d}). Renew it (e.g. sede.fnmt.gob.es) and re-run setup.")
                store.kv_set("cert:last_warn", time.time())
        except Exception as e:  # noqa: BLE001
            store.event("system", "sync_failing", f"Cannot open certificate '{profile.name}': {e}")

        rid = store.run_start(SOURCE)
        try:
            stats = client.sync(profile, store, **kw)
            store.run_end(rid, True, stats=stats)
            result = {"ok": True, **stats}
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            store.run_end(rid, False, error=err + "\n" + traceback.format_exc()[-2000:])
            result = {"ok": False, "error": err}
            if (time.time() - (store.last_ok(SOURCE) or 0) > FAIL_ALERT_AFTER_H * 3600
                    and time.time() - store.kv_get("fail_warn", 0) > 12 * 3600):
                store.event("system", "sync_failing", f"{SOURCE.upper()} sync failing for >{FAIL_ALERT_AFTER_H}h: {err[:160]}")
                store.kv_set("fail_warn", time.time())
            store.commit()
        if notify_user:
            result["notified"] = notify.deliver(store, profile.name)
        return result
    finally:
        store.close()
