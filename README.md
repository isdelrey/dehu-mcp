# dehu-mcp

An MCP server and background sync for **[DEHú](https://dehu.redsara.es)**, the Spanish government's single inbox for official notifications (*notificaciones*) and communications (*comunicaciones*) from AEAT, Seguridad Social, DGT, Extranjería, ministries, councils and others.

It logs in **by itself with your digital certificate** (`.p12` / `.pfx`). There's no Cl@ve Móvil, no browser and no human step. It keeps a local copy of everything, downloads the PDFs, alerts you when something new arrives, and lets an AI assistant (Claude, etc.) answer questions like *"Do I have anything pending in DEHú?"* or *"What did Extranjería send me last month?"*

> ⚠️ **Pending notifications are never opened automatically.** Under Spanish law, opening a notification counts as being notified, and legal deadlines start running. If you never open it, it's deemed notified 10 calendar days after it was made available. This tool only *lists* pending notifications and alerts you. You decide when to open them, in DEHú.

Unofficial; not affiliated with the Spanish administration. Everything runs locally: your certificate, password and documents never leave your machine, except to log in to DEHú and Cl@ve themselves.

---

## 🤖 Install with an AI assistant

Paste this repo's URL into your AI coding assistant (Claude Code, Cursor, etc.) and ask it to install it. **Instructions for the AI:**

1. **Requirements:** check that [`uv`](https://docs.astral.sh/uv/) is installed (`uv --version`). If not, install it with `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux) or `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows).
2. **Ask the user for the path to their certificate file** (`.p12` or `.pfx`, e.g. an FNMT *certificado de persona física*). Don't search their disk for it without asking.
3. **Register the certificate.** It asks for the certificate password, which **the user must type themselves**. Never ask the user to paste the password into the chat. In Claude Code, tell the user to run it with the `!` prefix so the prompt appears in their terminal:
   ```sh
   uvx --from git+https://github.com/isdelrey/dehu-mcp dehu-mcp setup --p12 "/path/to/certificate.p12"
   ```
   This stores the password in the OS keyring (macOS Keychain / Windows Credential Manager / Linux Secret Service) and copies the certificate into the app's private data folder. Optional flags: `--docs-dir` (where PDFs go; default `~/Documents/DEHU`) and `--profile <name>` (to configure several people).
4. **First import** (downloads history, about 1–2 minutes; doesn't fire alerts for old items):
   ```sh
   uvx --from git+https://github.com/isdelrey/dehu-mcp dehu-mcp sync --no-notify
   ```
5. **Register the MCP server:**
   - Claude Code:
     ```sh
     claude mcp add --scope user dehu -- uvx --from git+https://github.com/isdelrey/dehu-mcp dehu-mcp serve
     ```
   - Other MCP clients (Claude Desktop, Cursor, …), in their JSON config:
     ```json
     { "mcpServers": { "dehu": { "command": "uvx", "args": ["--from", "git+https://github.com/isdelrey/dehu-mcp", "dehu-mcp", "serve"] } } }
     ```
6. **Optional: background sync + alerts.** Ask the user first, because it installs a recurring job:
   ```sh
   uvx --from git+https://github.com/isdelrey/dehu-mcp dehu-mcp install --hours 4
   ```
   On macOS this installs a launchd agent; on Linux/Windows it prints a cron line to add. For phone pushes, set `DEHU_MCP_NTFY_URL=https://ntfy.sh/<long-random-topic>` before running `install`.
7. **Verify:** `uvx --from git+https://github.com/isdelrey/dehu-mcp dehu-mcp status`. The MCP tools appear after restarting the client or opening a new session.

---

## What you get

**MCP tools**

| Tool | What it does |
|---|---|
| `certificate_status` | Which certificate is configured, its expiry date, recent sync runs |
| `sync_now` | Logs in and refreshes now (`since=YYYY-MM-DD` to import more history) |
| `pending_notifications` | Notifications waiting to be opened (listed only, never opened) |
| `list_notifications` | Notifications already accepted or expired, filterable by date and issuing body |
| `list_communications` | Informative communications (`PENDIENTE` = unread) |
| `document_text` | Text of a downloaded PDF (plus its signed delivery receipt) |
| `read_communication` | Marks an unread *communication* as read and downloads it. Requires `confirm=true`. Never used for notifications |
| `recent_changes` | What's new since N hours ago |

**Background sync and alerts** (macOS Notification Center or Linux `notify-send`, plus optional [ntfy](https://ntfy.sh) phone push):
- a new **pending notification**, with its expiry date;
- a new notification or communication;
- the certificate expiring within 60 days;
- sync failing for more than 24 hours.

**Files:** `~/Documents/DEHU/Notificaciones/` and `.../Comunicaciones/`, one PDF per item, plus the signed delivery receipt (`*_resguardo.pdf`), annexes, and a `.json` with metadata.

## How it works

- **Login:** the DEHú API → SAML request to Cl@ve (`pasarela.clave.gob.es`) → identity provider **AFIRMA** (certificate), with TLS client authentication using your certificate → SAML response → DEHú issues a JWT (valid about 10 minutes). It logs in again automatically whenever needed. Takes a few seconds.
- **Data:** DEHú's own JSON API (the one its web app uses). History is fetched in windows of about a month (an API limit).
- **Certificate:** the `.p12` is decrypted in memory. A temporary PEM copy exists only during a sync, in a `0700` folder, and is deleted afterwards. Legacy FNMT `.p12` files (RC2/3DES) are supported.
- **State:** SQLite in the app data folder (`~/Library/Application Support/dehu-mcp`, `%APPDATA%\dehu-mcp` or `~/.local/share/dehu-mcp`). The MCP server reads from it, so it's fast and works even when DEHú is down. Only `sync_now` and `read_communication` touch the network.

## CLI

```
dehu-mcp setup --p12 FILE [--profile NAME] [--docs-dir DIR] [--password-stdin]
dehu-mcp sync [--since YYYY-MM-DD] [--no-download] [--no-notify] [--profile NAME]
dehu-mcp status | mark-seen | install [--hours N] | uninstall | serve
```
Environment variables: `DEHU_MCP_PROFILE`, `DEHU_MCP_HOME`, `DEHU_MCP_NTFY_URL`, `DEHU_MCP_P12_PASSWORD` (headless servers without a keyring).

Several people (e.g. you and a family member with an apoderamiento or their own certificate): run `setup --profile <name>` once per certificate, then pass `profile` to the tools.

## Troubleshooting

- **`Unexpected page during Cl@ve login`:** Cl@ve changed something, or the certificate is revoked or expired. Check `dehu-mcp status` and open an issue.
- **`No certificate configured`:** run `dehu-mcp setup` (step 3).
- **Updating:** `uvx --refresh --from git+https://github.com/isdelrey/dehu-mcp dehu-mcp status`.

## Related

- [aeat-mcp](https://github.com/isdelrey/aeat-mcp): the same idea for the Agencia Tributaria sede (debts, payment plans, filed returns and whether they're paid).

MIT licensed.
