"""DEHú client: headless Cl@ve login with a client certificate + the DEHú JSON API.

Login: GET /api/login/login-clave-form -> SAML POST to pasarela.clave.gob.es -> choose IdP
"AFIRMA" (certificate) -> pasarela-ident does TLS client auth -> SAMLResponse back to DEHú
login-check -> redirect to /es/login?authData=<JWT>. That JWT is the API Bearer token (~10 min);
we simply log in again when it expires (a login takes a few seconds).

SAFETY: this module never calls the "appearance"/accept endpoints of *pending* notifications.
Listing pending notifications has no legal effect; opening one does (it counts as notified).
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from . import cert as certmod
from .config import Profile
from .store import Store

BASE = "https://dehu.redsara.es"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
MAX_WINDOW_DAYS = 29  # API rejects ranges ~>1 month


class DehuError(RuntimeError):
    pass


class DehuClient:
    def __init__(self, profile: Profile, cert_paths: tuple[str, str]):
        self.profile = profile
        self.s = requests.Session()
        self.s.cert = cert_paths
        self.s.headers["User-Agent"] = UA
        self.token: str | None = None
        self.token_exp = 0.0

    # -- auth --------------------------------------------------------------
    def _submit(self, r: requests.Response, form, extra: dict | None = None) -> requests.Response:
        data = {i.get("name"): i.get("value", "") for i in form.find_all(["input", "textarea"]) if i.get("name")}
        data.update(extra or {})
        action = urljoin(r.url, data.get("idpUrl") or form.get("action") or r.url)
        o = urlparse(r.url)
        return self.s.request((form.get("method") or "get").upper(), action, data=data, timeout=60,
                              headers={"Origin": f"{o.scheme}://{o.netloc}", "Referer": r.url})

    def login(self) -> None:
        r = self.s.get(f"{BASE}/api/login/login-clave-form", timeout=60)
        r.raise_for_status()
        html = json.loads(r.text) if r.text.startswith('"') else r.text
        form = BeautifulSoup(html, "html.parser").find("form")
        if form is None:
            raise DehuError("DEHú login form not found")
        data = {i.get("name"): i.get("value", "") for i in form.find_all("input") if i.get("name")}
        r = self.s.post(form.get("action"), data=data, timeout=60,
                        headers={"Origin": BASE, "Referer": f"{BASE}/"})
        chose_idp = False
        for _ in range(12):
            if "authData=" in r.url:
                break
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            idp = soup.find("form", attrs={"name": "idpRedirect"}) or soup.find("form", id="idpRedirect")
            if idp is not None and not chose_idp:
                r, chose_idp = self._submit(r, idp, {"SelectedIdP": "AFIRMA"}), True
                continue
            forms = [f for f in soup.find_all("form") if (f.get("id") or f.get("name")) != "idpLanguage"]
            saml = [f for f in forms if f.find("input", attrs={"name": re.compile(r"SAML|ClaveToken|RelayState", re.I)})]
            if saml:
                r = self._submit(r, saml[0])
                continue
            text = soup.get_text(" ", strip=True)[:300]
            raise DehuError(f"Unexpected page during Cl@ve login at {r.url[:100]}: {text}")
        q = parse_qs(urlparse(r.url).query)
        if "authData" not in q:
            raise DehuError(f"Cl@ve login did not return authData (ended at {r.url[:100]})")
        self.token = q["authData"][0]
        payload = json.loads(base64.urlsafe_b64decode(self.token.split(".")[1] + "=="))
        if payload.get("username") != self.profile.nif:
            raise DehuError(f"Logged in as {payload.get('username')}, expected {self.profile.nif}")
        self.token_exp = float(payload.get("exp", time.time() + 540))

    def _ensure(self) -> None:
        if not self.token or time.time() > self.token_exp - 60:
            self.login()

    def api(self, method: str, path: str, **kw) -> requests.Response:
        for attempt in range(2):
            self._ensure()
            r = self.s.request(method, f"{BASE}/api/{path.lstrip('/')}", timeout=90,
                               headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"}, **kw)
            if r.status_code == 401 and attempt == 0:
                self.token = None
                continue
            return r
        return r

    def get_json(self, path: str):
        r = self.api("GET", path)
        if r.status_code != 200:
            raise DehuError(f"GET {path[:80]} -> {r.status_code} {r.text[:200]}")
        return r.json()

    # -- listing -----------------------------------------------------------
    @staticmethod
    def _windows(since: dt.date, until: dt.date):
        s = since
        while s <= until:
            e = min(s + dt.timedelta(days=MAX_WINDOW_DAYS), until)
            yield s, e
            s = e + dt.timedelta(days=1)

    @staticmethod
    def _d(x: dt.date) -> str:
        return x.strftime("%d/%m/%Y")

    def _paged(self, base_q: str, path: str) -> list[dict]:
        out, page = [], 1
        while True:
            j = self.get_json(f"{path}?{base_q}&page={page}&limit=50")
            items = j.get("items", []) if isinstance(j, dict) else j
            out.extend(items)
            if len(items) < 50:
                return out
            page += 1

    def pending_notifications(self) -> list[dict]:
        q = ("emitterEntityCode=&bondType=&titularNif=&vinculoReceptor=&postalDelivery=&publicId="
             "&availabilityDate%5Bleft_date%5D=&availabilityDate%5Bright_date%5D="
             "&expirationDate%5Bleft_date%5D=&expirationDate%5Bright_date%5D=")
        return self._paged(q, "v1/notifications")

    def realized_notifications(self, since: dt.date, until: dt.date) -> list[dict]:
        out = []
        for s, e in self._windows(since, until):
            q = ("emitterEntityCode=&state=&publicId=&titularNif=&bondType=&vinculoReceptor=&postalDelivery="
                 f"&finalDate%5Bleft_date%5D={self._d(s)}&finalDate%5Bright_date%5D={self._d(e)}"
                 "&expirationDate%5Bleft_date%5D=&expirationDate%5Bright_date%5D=")
            out += self._paged(q, "v1/realized_notifications")
        return out

    def communications(self, since: dt.date, until: dt.date) -> list[dict]:
        out = []
        for s, e in self._windows(since, until):
            q = ("emitterEntityCode=&bondType=&titularNif=&publicId="
                 f"&availabilityDate%5Bleft_date%5D={self._d(s)}&availabilityDate%5Bright_date%5D={self._d(e)}")
            out += self._paged(q, "v1/communications")
        return out

    # -- documents ---------------------------------------------------------
    def artifact(self, kind: str, ref: str, what: str) -> dict | None:
        """kind: realized_notifications|communications; what: document|voucher|annexe|full_detail"""
        r = self.api("GET", f"v1/{kind}/{ref}/{what}")
        if r.status_code == 200:
            return r.json()
        return {"_error": r.status_code, "_body": r.text[:300]}

    def mark_communication_read(self, ref: str) -> int:
        return self.api("PUT", f"v1/communications/{ref}").status_code


# ---------------------------------------------------------------------------
def _short(entity: str | None) -> str:
    e = entity or ""
    if "Tributaria" in e:
        return "AEAT"
    if "Extranjer" in e:
        return "Extranjeria-" + ("BCN" if "Barcelona" in e else re.sub(r"\W+", "", e.split(" en ")[-1])[:12])
    if "ndic" in e:
        return "Sindic"
    if "Seguridad Social" in e:
        return "SegSocial"
    return re.sub(r"\W+", "-", e)[:30] or "Organismo"


def doc_basename(item: dict) -> str:
    date = (item.get("availabilityDate") or "")[:10]
    return f"{date}_{_short(item.get('emitterEntity'))}_{item.get('identifier') or item['sentReference'][:12]}"


def _save_b64(path: Path, b64: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(b64))


def download_item(client: DehuClient, profile: Profile, kind: str, item: dict) -> list[str]:
    """Download document (+voucher, annexes) of a realized notification / readable communication."""
    folder = Path(profile.docs_dir) / ("Notificaciones" if kind == "realized_notifications" else "Comunicaciones")
    base, ref, saved = doc_basename(item), item["sentReference"], []
    doc = client.artifact(kind, ref, "document")
    if doc and doc.get("content"):
        ext = Path(doc.get("name") or "x.pdf").suffix or ".pdf"
        _save_b64(folder / f"{base}{ext}", doc["content"])
        saved.append(str(folder / f"{base}{ext}"))
    elif doc and doc.get("documentLink"):
        try:
            rr = requests.get(doc["documentLink"], timeout=60)
            if rr.ok and rr.content[:4] == b"%PDF":
                (folder / f"{base}.pdf").write_bytes(rr.content)
                saved.append(str(folder / f"{base}.pdf"))
        except requests.RequestException:
            pass
    if kind == "realized_notifications":
        v = client.artifact(kind, ref, "voucher")
        if v and v.get("content"):
            _save_b64(folder / f"{base}_resguardo.pdf", v["content"])
            saved.append(str(folder / f"{base}_resguardo.pdf"))
    al = client.artifact(kind, ref, "annexe") or {}
    for i, a in enumerate(al.get("annexes") or []):
        aref = a.get("annexeRef") or a.get("reference") or a.get("id")
        ad = client.artifact(kind, ref, f"annexe_document/{aref}")
        if ad and ad.get("content"):
            nm = re.sub(r"[^\w.\-]+", "_", ad.get("name") or f"anexo{i}.pdf")
            _save_b64(folder / f"{base}_anexo_{nm}", ad["content"])
            saved.append(str(folder / f"{base}_anexo_{nm}"))
    meta = folder / f"{base}.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps(item, ensure_ascii=False, indent=1))
    return saved


def add_sync_args(parser) -> None:
    parser.add_argument("--since", help="first sync: fetch history from this date (YYYY-MM-DD; default 3 years back)")
    parser.add_argument("--no-download", action="store_true", help="metadata only, don't download PDFs")


def sync_kwargs(a) -> dict:
    return {"download": not a.no_download, "since": a.since}


def sync(profile: Profile, store: Store, download: bool = True, since: str | None = None) -> dict:
    today = dt.date.today()
    last = store.kv_get("dehu:last_until")
    if since:
        since = dt.date.fromisoformat(since)
    elif last:
        since = dt.date.fromisoformat(last) - dt.timedelta(days=40)
    else:
        since = dt.date.fromisoformat(profile.options.get("since") or (today - dt.timedelta(days=3 * 365)).isoformat())
    stats = {"pending": 0, "realized_new": 0, "comms_new": 0, "downloaded": 0}
    with certmod.pem_files(profile) as paths:
        c = DehuClient(profile, paths)
        c.login()

        # Pending: metadata only, never opened.
        seen = set()
        for n in c.pending_notifications():
            key = f"dehu:pending:{n['sentReference']}"
            seen.add(key)
            st, _ = store.upsert("dehu", "pending", key, n)
            stats["pending"] += 1
            if st == "new":
                exp = (n.get("expirationDate") or "")[:10]
                store.event("dehu", "dehu_pending_notification",
                            f"NEW PENDING notification from {n.get('emitterEntity')} — {n.get('concept')}. "
                            f"Not opened. Expires {exp or '?'} (opening it starts legal deadlines).", key, n)
        store.mark_missing("dehu", "pending", seen)  # moved to realized (or expired)

        # Realized notifications
        for n in c.realized_notifications(since, today):
            key = f"dehu:notif:{n['sentReference']}"
            st, prev = store.upsert("dehu", "notification", key, n)
            if st == "new":
                stats["realized_new"] += 1
                files = download_item(c, profile, "realized_notifications", n) if download else []
                stats["downloaded"] += len(files)
                store.event("dehu", "dehu_new_notification",
                            f"Notification {n.get('state')}: {n.get('emitterEntity')} — {n.get('concept')} "
                            f"(available {(n.get('availabilityDate') or '')[:10]})", key, {**n, "files": files})
            elif st == "changed" and prev and prev.get("state") != n.get("state"):
                store.event("dehu", "dehu_notification_state",
                            f"Notification {n.get('identifier')} state {prev.get('state')} → {n.get('state')}", key, n)

        # Communications
        for m in c.communications(since, today):
            key = f"dehu:comm:{m['sentReference']}"
            st, prev = store.upsert("dehu", "communication", key, m)
            if st == "new":
                stats["comms_new"] += 1
                files = []
                if download and m.get("state") not in ("PENDIENTE",):
                    files = download_item(c, profile, "communications", m)
                    stats["downloaded"] += len(files)
                store.event("dehu", "dehu_new_communication",
                            f"Communication from {m.get('emitterEntity')} — {m.get('concept')} "
                            f"({(m.get('availabilityDate') or '')[:10]}, {m.get('state')})", key, {**m, "files": files})
        store.kv_set("dehu:last_until", today.isoformat())
        store.commit()
    return stats
