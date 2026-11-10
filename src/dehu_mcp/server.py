"""MCP server for DEHú — Dirección Electrónica Habilitada única (Spanish government notifications).

Reads from the local store (fast, works when DEHú is down). Only `sync_now` / `read_communication`
contact DEHú. Pending notifications are never opened: opening one has legal effects.
"""
from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import APP_NAME, cert as certmod, client, config, runner
from .pdftext import pdf_text

mcp = FastMCP(
    "dehu",
    instructions=(
        "Spanish government notifications (DEHú) for the configured certificate holder. Data comes from a local "
        "store refreshed by a background sync; call sync_now for fresh data. PENDING notifications are never "
        "opened by these tools: opening one counts as legally notified and starts deadlines (if never opened, it "
        "is deemed notified 10 calendar days after being made available) — tell the user and let them decide. "
        f"If nothing is configured, tell the user to run `{APP_NAME} setup --p12 <file>` in a terminal."
    ),
)


def _ts(t):
    return dt.datetime.fromtimestamp(t).isoformat(timespec="minutes") if t else None


def _profile(name):
    try:
        return config.get_profile(name), None
    except config.ConfigError as e:
        return None, {"configured": False, "how_to_configure": str(e)}


@mcp.tool()
def certificate_status(profile: str | None = None) -> dict:
    """Configured certificate/profile, its expiry, and recent sync runs."""
    prof, err = _profile(profile)
    if err:
        return err
    out = {"configured": True, "profile": prof.name, "nif": prof.nif, "documents_dir": prof.docs_dir,
           "all_profiles": [p.name for p in config.list_profiles()]}
    try:
        ci = certmod.info(prof)
        out["certificate"] = {"subject": ci.subject, "valid_until": ci.not_after.date().isoformat(), "days_left": ci.days_left}
    except Exception as e:  # noqa: BLE001
        out["certificate_error"] = str(e)
    st = runner.open_store(prof)
    try:
        out["last_runs"] = [{"started": _ts(r["started"]), "ok": bool(r["ok"]), "stats": r["stats"],
                             "error": (r["error"] or "").splitlines()[0] if r["error"] else None} for r in st.last_runs(limit=5)]
    finally:
        st.close()
    return out


@mcp.tool()
def sync_now(download_documents: bool = True, since: str | None = None, profile: str | None = None) -> dict:
    """Log in to DEHú with the certificate (headless Cl@ve) and refresh notifications and communications.
    Downloads PDFs of realized notifications and read communications. since=YYYY-MM-DD to (re)import history."""
    prof, err = _profile(profile)
    if err:
        return err
    return runner.run(prof, notify_user=False, download=download_documents, since=since)


@mcp.tool()
def pending_notifications(profile: str | None = None) -> dict:
    """Notifications waiting to be opened (as of the last sync). Only lists them — opening is a legal act."""
    prof, err = _profile(profile)
    if err:
        return err
    st = runner.open_store(prof)
    try:
        items = st.items("dehu", "pending")
        keys = ("emitterEntity", "concept", "availabilityDate", "expirationDate", "identifier", "sentReference")
        return {"count": len(items),
                "warning": "Opening a pending notification counts as notified and starts deadlines. If never opened, "
                           "it is deemed notified 10 calendar days after availability.",
                "items": [{k: i.get(k) for k in keys} for i in items]}
    finally:
        st.close()


def _list(profile, kind, since, emitter, limit):
    prof, err = _profile(profile)
    if err:
        return err
    st = runner.open_store(prof)
    try:
        items = st.items("dehu", kind)
        if since:
            items = [i for i in items if (i.get("availabilityDate") or "") >= since]
        if emitter:
            items = [i for i in items if emitter.lower() in (i.get("emitterEntity") or "").lower()]
        items.sort(key=lambda i: i.get("availabilityDate") or "", reverse=True)
        keys = ("availabilityDate", "finalDate", "state", "emitterEntity", "concept", "identifier", "sentReference")
        return {"count": len(items), "items": [{k: i.get(k) for k in keys} for i in items[:limit]]}
    finally:
        st.close()


@mcp.tool()
def list_notifications(since: str | None = None, emitter: str | None = None, limit: int = 50,
                       profile: str | None = None) -> dict:
    """Realized (accepted or expired) notifications, newest first. since=YYYY-MM-DD; emitter = substring
    of the issuing body (e.g. 'Tributaria', 'Extranjer', 'Seguridad Social')."""
    return _list(profile, "notification", since, emitter, limit)


@mcp.tool()
def list_communications(since: str | None = None, emitter: str | None = None, limit: int = 50,
                        profile: str | None = None) -> dict:
    """Communications (informative, no legal effect), newest first. state PENDIENTE = unread."""
    return _list(profile, "communication", since, emitter, limit)


def _files_for(prof, ref):
    st = runner.open_store(prof)
    try:
        item = st.get(f"dehu:notif:{ref}") or st.get(f"dehu:comm:{ref}")
    finally:
        st.close()
    if not item:
        return []
    base = client.doc_basename(item)
    out = []
    for sub in ("Notificaciones", "Comunicaciones"):
        out += [str(p) for p in sorted((Path(prof.docs_dir) / sub).glob(f"{base}*")) if p.suffix.lower() == ".pdf"]
    return out


@mcp.tool()
def document_text(sent_reference: str, include_receipt: bool = False, profile: str | None = None) -> dict:
    """Text of a downloaded notification/communication PDF (by sentReference) plus its file paths."""
    prof, err = _profile(profile)
    if err:
        return err
    files = _files_for(prof, sent_reference)
    if not files:
        return {"error": "No downloaded document for that sentReference (run sync_now; unread communications "
                         "are only downloadable after read_communication)."}
    main = [f for f in files if "_resguardo" not in f and "_anexo_" not in f]
    out = {"files": files, "text": pdf_text(main[0])[:60_000] if main else ""}
    if include_receipt:
        rec = [f for f in files if "_resguardo" in f]
        out["receipt_text"] = pdf_text(rec[0])[:20_000] if rec else None
    return out


@mcp.tool()
def read_communication(sent_reference: str, confirm: bool = False, profile: str | None = None) -> dict:
    """Mark an UNREAD communication as read in DEHú and download it. Communications have no legal effect,
    but this changes state in the user's account — only call with confirm=True after the user agrees.
    Never used for notifications."""
    if not confirm:
        return {"needs_confirmation": True,
                "message": "This marks the communication as read in DEHú. Ask the user, then call again with confirm=true."}
    prof, err = _profile(profile)
    if err:
        return err
    st = runner.open_store(prof)
    try:
        item = st.get(f"dehu:comm:{sent_reference}")
        if not item:
            return {"error": "Unknown communication (run sync_now first)."}
        with certmod.pem_files(prof) as paths:
            c = client.DehuClient(prof, paths)
            code = c.mark_communication_read(sent_reference)
            files = client.download_item(c, prof, "communications", item)
        item["state"] = "LEIDA"
        st.upsert("dehu", "communication", f"dehu:comm:{sent_reference}", item)
        st.commit()
        return {"marked_read_http_status": code, "files": files, "text": pdf_text(files[0])[:60_000] if files else ""}
    finally:
        st.close()


@mcp.tool()
def recent_changes(since_hours: float = 168, profile: str | None = None) -> dict:
    """What changed (new pending notifications, new notifications/communications, sync problems) in the last N hours."""
    prof, err = _profile(profile)
    if err:
        return err
    st = runner.open_store(prof)
    try:
        evs = st.events(since_ts=time.time() - since_hours * 3600, limit=500)
        return {"events": [{"at": _ts(e["ts"]), "type": e["type"], "summary": e["summary"], "key": e["key"]} for e in evs]}
    finally:
        st.close()
