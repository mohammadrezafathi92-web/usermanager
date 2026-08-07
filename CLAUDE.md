# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A VPN reseller panel ("یوزر منیجر یکپارچه") that gives every customer a single **shared quota** (`total_quota_bytes`/`used_bytes`) spread across however many connections they have, mixing protocols freely: WireGuard/OpenVPN/L2TP (all provisioned on MikroTik RouterOS nodes) and/or V2Ray/Xray (via SSH or a 3X-UI panel API). When the shared quota runs out or the expiry date passes, every one of that customer's connections is disabled automatically, on every node, without an admin having to do it manually.

Bundled into the same backend process (no separate services, no manual `.env` wiring beyond secrets):
- A full-featured **Telegram bot** (sales/support/admin) — runs as a background thread inside the FastAPI process.
- A real **RADIUS server** (RFC 2865/2866) for OpenVPN/L2TP PPP auth+accounting — MikroTik routers point `/radius` at this panel instead of using local PPP secrets.
- Sellable **packages** (quota + duration + price + bundled services + optional file/text), a per-customer **wallet balance**, an **admin/tutorial** library, scheduled **DB backups** sent to Telegram, a public **subscription panel** per customer (no login), and a **3-tier reseller hierarchy** (superadmin → admin → seller).
- A separate **external bot API** (`/api/bot`, `X-API-Key` auth) for third-party sales bots — it's the exact same API the built-in bot itself uses internally, so it's always complete and up to date.

The full Persian README (`README.md`) is the source of truth for deployment/operational details (RADIUS setup on MikroTik, Xray SSH vs 3X-UI config, env vars, security notes, troubleshooting) — read it before making changes to provisioning, RADIUS, or deploy flows. This file focuses on things a coding agent needs that aren't already there.

## Commands

There is no test suite, linter, or CI in this repo (confirmed absent, not just undocumented) — the closest thing to verification is a manual compile/parse check, described below.

**Backend (FastAPI, Python 3, SQLite via SQLAlchemy):**
```bash
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then edit SECRET_KEY / DEFAULT_ADMIN_PASSWORD
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
Sanity-check backend edits compile before considering them done (there's no pytest to run instead):
```bash
python3 -m py_compile $(find app -name "*.py")
```

**Frontend (React + Vite + Tailwind, RTL/Persian UI):**
```bash
cd frontend
npm install
npm run dev      # dev server
npm run build    # production build -> frontend/dist
npm run preview
```
No eslint/prettier config exists. If Vite/esbuild binaries in your environment don't match the host platform, parse-check edited `.jsx`/`.js` files with `@babel/parser` (`sourceType: "module", plugins: ["jsx"]`) instead of trying to run the dev server.

**Full stack via Docker (how it's actually deployed):**
```bash
docker compose up -d --build
# single side only:
docker compose up -d --build backend   # or: frontend
docker compose logs -f backend         # RADIUS, quota polling, and bot logs all land here
```
Panel on `:80`, API on `:8000`, RADIUS on `1812/1813` UDP. There's also a one-command installer (`install.sh` / `install_from_windows.ps1`) and an `update.sh` for the day-to-day "pull latest, rebuild" cycle on an already-deployed server — see README for both.

## Database migrations: don't write migration scripts

`main.py`'s `_auto_migrate_missing_columns()` runs on every startup and diffs the SQLAlchemy models against the live SQLite schema, adding any missing **columns and indexes** with `ALTER TABLE`/`CREATE INDEX` automatically (additive-only — never drops/renames/touches existing data, and only SQLite is supported). This means: adding a new `Column(...)` to an existing model in `models.py` is the entire migration — no Alembic, no hand-written SQL, no manual `ALTER TABLE` step, no migration file to create. Brand new tables are handled by the ordinary `Base.metadata.create_all()` call right before it. Historical one-off data backfills (e.g. `_backfill_hierarchy_node_access`) follow the same "gate on `table_is_new`, run once, never touch it again" pattern in `main.py` — model that pattern if a new feature needs one.

## Architecture

**Startup sequence (`main.py:on_startup`)** matters: insecure-default warnings → auto-migrate → one-time hierarchy/permission-group seeding → first-run admin bootstrap → then, unless this is a `bot_standalone_mode` instance or a passive HA standby, `_start_full_services()` wires up the APScheduler jobs (quota polling, RADIUS session cleanup, daily notify, 4x/day backup), starts the RADIUS server, and starts the Telegram bot(s).

**Background execution model** — three long-running things share the one process, each isolated from request/response handling:
- The **quota poller** (`services/quota_manager.poll_all`, APScheduler interval job) visits every node, pulls each connection's usage delta, adds it to the owning user's `used_bytes`, and disables anything that just crossed its quota/expiry — the single enforcement choke point for both this poller and RADIUS accounting.
- The **RADIUS server** (`services/radius_server.py`) runs on its own thread/socket, authenticates/accounts OpenVPN and L2TP PPP sessions, and only supports PAP/CHAP (no MS-CHAPv2 — a real router-config gotcha, see README).
- The **Telegram bot** (`telegram_bot/runner.py`) runs on its own thread with its own event loop. It talks to the DB through `telegram_bot/panel_bridge.py` **in-process** (direct DB session, zero HTTP) by default. If the bot is instead deployed to a *second* server (via the panel's "نصب ربات روی سرور دیگر", `services/remote_deploy.py`), that second instance runs with `BOT_STANDALONE_MODE=true` and the exact same bot `handlers/` code transparently switches to talking over HTTP (`telegram_bot/remote_bridge.py`, `X-API-Key`-authenticated `/api/bot` calls) instead — the handler code itself never knows or cares which bridge it's using. Multiple bots can run concurrently in one process: the shared bot, plus one per level-2 Admin who's configured their own dedicated bot token.

**3-tier reseller hierarchy** (`services/hierarchy.py`, `permissions.py`) — fixed at exactly 3 levels: `superadmin` → `admin` (level 2, full panel access but scoped to their own tree — created directly by a superadmin) → `seller` (level 3, gated by granular `permissions.PERMISSION_CHOICES` checkboxes, works within their parent admin's packages/nodes, created by that admin). Every router that needs "which admin(s) can this caller see" should go through `hierarchy.owned_admin_ids()`/`hierarchy.role()` rather than re-deriving the logic — a superadmin does **not** implicitly see every customer; each admin's own customer base is private to that admin's tree by design, only their own directly-created users are visible to a superadmin the normal way. `deps.py`'s `require_permission`/`require_admin_or_above`/`require_superadmin` are the FastAPI dependency gates built on top of this.

**Two parallel "who's calling" auth systems**, never mixed:
- Panel admins: JWT bearer tokens (`deps.get_current_admin`, `security.py`), issued by `/api/auth/login`.
- Everything under `/api/bot`: a static `X-API-Key` header checked against the `ApiKey` table (`deps.get_bot_api_key`) — this is what both the built-in Telegram bot (in remote mode) and any third-party sales bot use, with zero notion of an admin session.
- The customer-facing subscription panel (`routers/subscription.py`, `/api/subscribe/{token}`) is a **third**, deliberately even lighter model: no header/token scheme at all, just a long unguessable per-user `subscription_token` embedded directly in the URL, checked with a plain DB lookup. It intentionally lives outside the `/api/users` router (which gates its whole prefix behind `get_current_admin`) for exactly this reason.

**Quota/expiry is tracked at two independent levels** — don't assume `User.total_quota_bytes`/`used_bytes`/`expire_at` is the only source of truth. A `Purchase` row (see its docstring in `models.py`) gives one specific package-purchase its *own* independently-enforced quota/usage/expiry, used when a package is added on top of an existing user via "افزودن پکیج" rather than at initial user creation; `Connection.purchase_id`/`purchase_batch` link a connection back to the `Purchase` (or group) that created it. Code that touches usage/expiry enforcement generally needs to handle both the user-level and purchase-level cases.

**Node integration is per-protocol, not per-node-type**: a MikroTik node can host WireGuard, OpenVPN, and L2TP simultaneously (`services/mikrotik_client.py`, RouterOS API); an Xray node speaks either raw SSH (`services/xray_client.py`, edits `config.json` directly) or the 3X-UI web panel API (`services/threexui_client.py`) — chosen per-node at add/edit time, not a global setting. `services/link_builder.py` builds the human-readable config text/QR-payload for each protocol type, and `services/user_ops.get_connection_share()` is the one shared function that wraps all of them into a uniform `{kind, link, config_text, server, port, username, password, psk}` shape — reuse it rather than re-deriving per-protocol share logic (both `routers/users.py`'s admin share endpoint and `routers/subscription.py`'s public endpoint call straight into it).

**HA (high availability)** is optional (`PanelSettings.ha_enabled`), primary/standby, poll-based (`main.py`'s `ha_tick`, every 20s) rather than a real cluster: a standby health-checks + pulls a full DB snapshot from its peer, and after `HA_FAILOVER_THRESHOLD` (5) consecutive failures auto-promotes itself and starts full services — but never auto-fails-back; that's a manual `/api/ha/resolve` step by design (split-brain guard). If touching this, read `ha_tick`'s docstring in `main.py` first — it documents a real bug that was found and fixed around session/connection lifetime across the DB-file-swap that a pulled snapshot does.

**Frontend** is a single-page app (`frontend/src/App.jsx`) with three auth-gate wrapper components — `Protected` (any logged-in admin), `AdminOrAboveOnly` (level-2 Admin or superadmin), `PermRoute` (a specific permission) — wrapping most routes. The one exception is `/s/:token` (`pages/Subscription.jsx`), which must stay **outside** all three since it's the public customer panel matching `routers/subscription.py` on the backend. `api/client.js` is a single flat file of axios call wrappers (no per-resource split) with a 401 interceptor that redirects to `/login` — the public subscription page reuses the same `client` instance since its calls never require/trigger auth. `i18n/translations.js` holds every UI string as flat `"section.key"` string keys per language (`fa`/`en`), looked up via `translate(lang, key)`/`useLanguage()`'s `t()` — not nested objects, so search for the literal dotted key when adding or changing UI text.
