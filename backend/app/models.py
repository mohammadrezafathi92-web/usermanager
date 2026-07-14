import enum
import datetime as dt
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    BigInteger,
)
from sqlalchemy.orm import relationship

from .database import Base


def now():
    return dt.datetime.utcnow()


class NodeType(str, enum.Enum):
    mikrotik = "mikrotik"
    xray = "xray"


class ConnectionType(str, enum.Enum):
    wireguard = "wireguard"  # hosted on a MikroTik node
    openvpn = "openvpn"  # hosted on a MikroTik node (PPP secret)
    l2tp = "l2tp"  # hosted on a MikroTik node (PPP secret)
    ikev2 = "ikev2"  # hosted on a MikroTik node (PPP secret via RADIUS, same as l2tp)
    sstp = "sstp"  # hosted on a MikroTik node (PPP secret via RADIUS, same as l2tp/ikev2)
    xray = "xray"  # vless/vmess/trojan hosted on an Xray node


class UserStatus(str, enum.Enum):
    active = "active"
    disabled = "disabled"  # manually disabled by admin
    quota_exceeded = "quota_exceeded"
    expired = "expired"


class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=now)

    # True for the main/original admin (and any admin explicitly promoted) -
    # sees/manages every group's users and every panel section regardless
    # of `permissions` below, and is the only role allowed to manage other
    # admins. The very first admin ever created (see main.py's bootstrap)
    # is always a superadmin.
    is_superadmin = Column(Boolean, default=False, nullable=False)

    # Comma-separated subset of PERMISSION_CHOICES (see app/permissions.py)
    # - which extra panel sections this admin can see/use, on top of always
    # being able to manage users in their own group (see User.owner_admin_id
    # below). Ignored entirely for superadmins. Empty/null = only user
    # management.
    permissions = Column(Text, nullable=True, default="")

    # Optional custom slug for this admin's own login link (see
    # frontend route /a/:slug) - purely a bookmarkable/brandable shortcut to
    # the same login page, NOT a real subdomain/DNS entry and NOT itself a
    # security boundary (the actual access control is `is_superadmin` +
    # `permissions` + `User.owner_admin_id`, enforced server-side on every
    # request regardless of which URL was used to log in).
    login_slug = Column(String(64), unique=True, nullable=True)

    # Wholesale/reseller credit balance in tomans - a non-superadmin spends
    # this (at Package.cooperation_price, see below) when creating a
    # package-based user for their own group from the panel, instead of
    # that package's full customer-facing price. Topped up manually by a
    # superadmin from the "مدیریت ادمین‌ها" page. Always ignored for
    # superadmins (they're never charged).
    balance = Column(BigInteger, default=0, nullable=False)

    # Lets this admin manage their OWN group's users directly from the
    # Telegram bot (see telegram_bot/admin_scope.py) - independent of the
    # bot's global BotSettings.admin_ids list, and scoped so they only ever
    # see/create/edit users with owner_admin_id == this admin's id. NULL =
    # this admin has no bot access of their own.
    telegram_id = Column(BigInteger, unique=True, nullable=True)

    # Optional link to a reusable AdminPermissionGroup (see below). When
    # set, this admin's effective permissions come from the GROUP instead
    # of the `permissions` column above (see permissions.effective_permissions)
    # - lets a superadmin define e.g. "پشتیبان"/"فروش" templates once and
    # apply/edit them for many admins at once, instead of re-checking the
    # same boxes per admin. NULL (the default, and what every admin created
    # before this feature existed still has) keeps the old per-admin
    # `permissions` behavior exactly as it was.
    group_id = Column(Integer, ForeignKey("admin_permission_groups.id", ondelete="SET NULL"), nullable=True)
    group = relationship("AdminPermissionGroup")

    # ---------- Usage-based billing (مورد ۶) ----------
    # "flat" (default, existing behavior) = this admin is charged a flat
    # price (Package.cooperation_price or price) out of `balance` above,
    # at the moment they create a package-based user - see
    # routers/users.py's _charge_admin_for_package.
    # "usage" = this admin is NOT charged anything at package-creation
    # time. Instead they're given a GB volume pool (volume_balance_gb
    # below) that depletes in near-real-time as their own users actually
    # consume traffic - see services/quota_manager.py's _apply_delta,
    # which is the single choke point every protocol's usage (WireGuard/
    # Xray polling, OpenVPN/L2TP/IKEv2 RADIUS accounting) already flows
    # through.
    billing_mode = Column(String(16), nullable=False, default="flat")
    # Remaining GB credit for "usage" mode admins - meaningless/ignored
    # for "flat" mode admins and superadmins. Can go negative (over-usage
    # isn't blocked mid-session, same philosophy as the money balance not
    # retroactively disabling already-provisioned users) - a superadmin
    # tops it up the same way as money, just in GB instead of tomans.
    volume_balance_gb = Column(Float, nullable=True, default=0)


class AdminPermissionGroup(Base):
    """A reusable, named set of permissions (see app/permissions.py) that
    can be assigned to several AdminUsers at once via AdminUser.group_id -
    edit the group once and every admin in it picks up the change,
    instead of re-checking the same boxes on each admin individually."""

    __tablename__ = "admin_permission_groups"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), unique=True, nullable=False)
    # Same comma-separated PERMISSION_CHOICES format as AdminUser.permissions
    permissions = Column(Text, nullable=True, default="")
    created_at = Column(DateTime, default=now)


class AdminBalanceLog(Base):
    """Audit trail for every manual change to an AdminUser's wholesale
    credit balance (see AdminUser.balance) - both the initial "اعتبار پایه"
    given at reseller creation and every later "افزایش/کاهش اعتبار" a
    superadmin performs from the ادمین‌ها page. Deliberately does NOT log
    automatic per-purchase deductions (_charge_admin_for_package in
    routers/users.py) - those are already visible via the user/purchase
    history itself; this table is specifically the superadmin-facing audit
    log of manual top-ups, per the "لاگ افزایش اعتبار" requirement."""

    __tablename__ = "admin_balance_logs"

    id = Column(Integer, primary_key=True, index=True)
    admin_id = Column(Integer, ForeignKey("admin_users.id"), nullable=False, index=True)
    # Signed delta applied to the balance - positive for a top-up, negative
    # for a manual correction/deduction. Never zero (the router rejects
    # zero-amount requests since they'd be a no-op log entry).
    amount = Column(BigInteger, nullable=False)
    # Snapshot of the resulting balance right after this change, so the
    # log reads correctly even if later entries are added/edited.
    balance_after = Column(BigInteger, nullable=False)
    note = Column(String(255), nullable=True)
    # Which superadmin performed this - NULL for the automatic "اعتبار
    # پایه" entry created at admin creation time (no separate "actor" then,
    # the creating superadmin is already implied by require_superadmin on
    # the create endpoint, but not modeled as a distinct actor here to
    # keep that first entry simple).
    created_by_id = Column(Integer, ForeignKey("admin_users.id"), nullable=True)
    created_at = Column(DateTime, default=now, index=True)

    admin = relationship("AdminUser", foreign_keys=[admin_id])
    created_by = relationship("AdminUser", foreign_keys=[created_by_id])


class AdminVolumeLog(Base):
    """Audit trail for manual changes to a "usage" billing_mode AdminUser's
    GB volume pool (AdminUser.volume_balance_gb) - the volume equivalent of
    AdminBalanceLog above. Deliberately does NOT log the automatic
    near-real-time depletion done by quota_manager.py's _apply_delta (that
    would be one row per poll cycle); only manual top-ups/corrections made
    by a superadmin from the ادمین‌ها page."""

    __tablename__ = "admin_volume_logs"

    id = Column(Integer, primary_key=True, index=True)
    admin_id = Column(Integer, ForeignKey("admin_users.id"), nullable=False, index=True)
    # Signed delta in GB - positive for a top-up, negative for a manual
    # correction/deduction. Never zero.
    amount_gb = Column(Float, nullable=False)
    # Snapshot of the resulting volume_balance_gb right after this change.
    balance_after_gb = Column(Float, nullable=False)
    note = Column(String(255), nullable=True)
    created_by_id = Column(Integer, ForeignKey("admin_users.id"), nullable=True)
    created_at = Column(DateTime, default=now, index=True)

    admin = relationship("AdminUser", foreign_keys=[admin_id])
    created_by = relationship("AdminUser", foreign_keys=[created_by_id])


class AdminLoginLog(Base):
    """Every admin panel login ATTEMPT (both successful and failed), for
    the superadmin's IP-based login report (routers/auth.py's /login
    writes one row per attempt). Deliberately includes the superadmin's
    OWN logins too - per the explicit requirement that the main admin
    should see themselves in this log as well, not just sub-admins."""

    __tablename__ = "admin_login_logs"

    id = Column(Integer, primary_key=True, index=True)
    # NULL when the typed username didn't match any admin at all (e.g. a
    # brute-force attempt with a made-up username) - still logged with the
    # raw attempted_username/ip so that noise is visible too, just without
    # a resolvable admin_id.
    admin_id = Column(Integer, ForeignKey("admin_users.id"), nullable=True, index=True)
    attempted_username = Column(String(64), nullable=True)
    ip_address = Column(String(64), nullable=True, index=True)
    user_agent = Column(String(255), nullable=True)
    success = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=now, index=True)

    admin = relationship("AdminUser")


class ApiKey(Base):
    """API keys used by external systems (e.g. a Telegram sales bot) to
    call the /api/bot/* endpoints without going through the admin JWT login."""

    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String(128), nullable=False)
    key = Column(String(128), unique=True, index=True, nullable=False)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=now)
    last_used_at = Column(DateTime, nullable=True)


class Node(Base):
    """A backend server: either a MikroTik router (RouterOS API) hosting
    WireGuard/OpenVPN/L2TP, or a server running Xray-core reachable over SSH."""

    __tablename__ = "nodes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    type = Column(Enum(NodeType), nullable=False)
    enabled = Column(Boolean, default=True)

    # --- MikroTik connection fields ---
    mt_host = Column(String(255), nullable=True)
    mt_port = Column(Integer, nullable=True, default=8728)  # plain API port
    mt_api_ssl_port = Column(Integer, nullable=True, default=8729)  # API-SSL port, used when mt_use_ssl is on
    mt_username = Column(String(128), nullable=True)
    mt_password = Column(String(255), nullable=True)
    mt_use_ssl = Column(Boolean, default=False)

    # Public endpoint clients should connect to (router WAN ip/host) - shared
    # by WireGuard / OpenVPN / L2TP since they usually live on the same router.
    mt_endpoint_host = Column(String(255), nullable=True)

    # --- MikroTik WireGuard ---
    mt_wireguard_interface = Column(String(128), nullable=True, default="wireguard1")
    mt_endpoint_port = Column(Integer, nullable=True, default=13231)
    mt_client_dns = Column(String(255), nullable=True, default="1.1.1.1")
    mt_client_subnet = Column(String(64), nullable=True, default="10.66.66.0/24")

    # --- MikroTik OpenVPN / L2TP (authenticated via RADIUS) ---
    # IMPORTANT: for OpenVPN/L2TP the panel ONLY manages the username/
    # password (now via RADIUS, previously via local PPP secrets). Everything
    # else - the IP pool, the OpenVPN server + certificate, and the L2TP/
    # IPsec server + PSK - is configured directly on the router by the admin,
    # not pushed by the panel. The fields below are only stored so the panel
    # can print the correct values into the client's config/instructions and
    # so it can (optionally) register itself as a RADIUS client on the
    # router; they must match whatever was actually configured on the router.
    mt_radius_secret = Column(String(255), nullable=True)  # shared secret for this router's /radius entry
    mt_ovpn_port = Column(Integer, nullable=True, default=1194)
    mt_ovpn_certificate = Column(String(128), nullable=True)  # informational only, shown in the generated .ovpn
    mt_l2tp_use_ipsec = Column(Boolean, default=True)
    mt_l2tp_ipsec_secret = Column(String(255), nullable=True)  # informational only, shown to the client

    # --- MikroTik IKEv2 (authenticated via RADIUS, same PPP-secret pattern
    # as L2TP - see the big comment above). IKEv2 peer/policy/certificate or
    # PSK setup is done directly on the router by the admin; these fields
    # are informational only, just so the panel can print correct
    # instructions to the client.
    mt_ikev2_psk = Column(String(255), nullable=True)  # pre-shared key, informational only, shown to the client

    # --- MikroTik SSTP (authenticated via RADIUS, same PPP-secret pattern as
    # L2TP/IKEv2 above). SSTP wraps PPP inside an HTTPS-like TLS tunnel (like
    # OpenVPN, it needs a server certificate, not a PSK) - the certificate
    # itself is configured directly on the router by the admin; these fields
    # are informational only, just so the panel can print correct
    # instructions to the client.
    mt_sstp_port = Column(Integer, nullable=True, default=443)
    mt_sstp_certificate = Column(String(128), nullable=True)  # informational only, shown in the generated config

    # --- Xray connection method ---
    # "ssh" (default, edits config.json over SSH + restarts the service) or
    # "3xui" (talks to a 3X-UI panel's own HTTP API - no SSH access needed,
    # used e.g. when Xray runs inside a MikroTik container).
    xr_panel_mode = Column(String(16), nullable=True, default="ssh")
    xr_panel_base_url = Column(String(255), nullable=True)  # e.g. http://1.2.3.4:2053/secretpath
    # Preferred: API token from the panel's Settings -> Authentication ->
    # API Token screen (Authorization: Bearer ...) - skips the login form
    # entirely, which is more reliable behind WAFs/reverse proxies. Falls
    # back to username/password session login if left blank.
    xr_panel_api_token = Column(String(255), nullable=True)
    xr_panel_username = Column(String(128), nullable=True)
    xr_panel_password = Column(String(255), nullable=True)
    xr_panel_inbound_id = Column(Integer, nullable=True)

    # --- Xray fields (managed over SSH) ---
    xr_ssh_host = Column(String(255), nullable=True)
    xr_ssh_port = Column(Integer, nullable=True, default=22)
    xr_ssh_username = Column(String(128), nullable=True)
    xr_ssh_password = Column(String(255), nullable=True)
    xr_ssh_private_key = Column(Text, nullable=True)
    xr_config_path = Column(String(255), nullable=True, default="/usr/local/etc/xray/config.json")
    xr_service_name = Column(String(128), nullable=True, default="xray")
    xr_api_address = Column(String(255), nullable=True, default="127.0.0.1:10085")
    xr_inbound_tag = Column(String(128), nullable=True, default="proxy")
    # public info clients need to build their share-link
    xr_public_host = Column(String(255), nullable=True)
    xr_public_port = Column(Integer, nullable=True, default=443)
    xr_network = Column(String(64), nullable=True, default="tcp")
    xr_security = Column(String(64), nullable=True, default="tls")
    xr_sni = Column(String(255), nullable=True)

    last_seen = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)

    connections = relationship("Connection", back_populates="node")


class User(Base):
    """An end customer. Quota is shared across ALL of their connections,
    regardless of protocol (WireGuard/OpenVPN/L2TP via MikroTik, or Xray)."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(128), unique=True, index=True, nullable=False)
    full_name = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)

    # Links this account to a Telegram user, so the sales/support bot can
    # recognize a returning customer on /start without them re-entering a
    # username. Null until the customer either registers via the bot or an
    # admin links an existing account to a chat id manually.
    # NOT unique on purpose - one Telegram account can be linked to several
    # User rows (a customer who bought more than once, ending up with
    # multiple separate accounts) - the bot shows an account picker when a
    # telegram_id resolves to more than one User (see routers/bot.py's
    # list_users_by_telegram and telegram_bot/handlers/customer.py's
    # _resolve_account). A pre-existing production DB will still have the
    # old UNIQUE index physically on disk until it's dropped once with a
    # migration (see fix_telegram_id_unique.py) - this column definition
    # alone does not retroactively change an already-created SQLite table.
    telegram_id = Column(BigInteger, nullable=True, index=True)

    # Which admin "owns" (manages) this customer - set once, at creation
    # time, from the creating admin's own id (a superadmin can pick a
    # different owner explicitly - see routers/users.py). Non-superadmin
    # admins only ever see/manage users where this matches their own id;
    # superadmins see everyone, optionally filtered by this field in the
    # panel's users list ("بر اساس ادمین"). NULL = unassigned/legacy user
    # (created before this feature, or by the bot's own auto-create paths -
    # see services/user_ops.py) - visible to superadmins only, same as if
    # it belonged to a since-deleted admin.
    owner_admin_id = Column(Integer, ForeignKey("admin_users.id"), nullable=True, index=True)

    # Which package this user was last created/renewed with, if any - set
    # automatically whenever a user is built from a package (single or bulk
    # create) or bulk-renewed by picking a package (see routers/users.py's
    # bulk_update_users + services/user_ops.py). NULL for users made
    # manually with no package, or legacy users predating this column.
    # Exists purely so the panel can filter/select users "by package" for
    # group actions (e.g. "disable everyone on the 20GB package") - it is
    # NOT re-validated against the package's current settings, so editing a
    # package later does not retroactively change users already on it.
    package_id = Column(Integer, ForeignKey("packages.id"), nullable=True, index=True)

    total_quota_bytes = Column(BigInteger, default=0)  # 0 == unlimited
    used_bytes = Column(BigInteger, default=0)

    # Wallet-style credit balance (tomans) the customer can top up via the
    # bot's "افزایش اعتبار" flow (card-to-card + admin approval, same as a
    # package purchase) and spend later - kept separate from quota/expiry.
    balance = Column(BigInteger, default=0)

    expire_at = Column(DateTime, nullable=True)  # null == never expires
    status = Column(Enum(UserStatus), default=UserStatus.active)

    # If set, expire_at is left null until the user's very first successful
    # RADIUS login (openvpn/l2tp), at which point the RADIUS auth handler
    # computes expire_at = first_login_time + this many days and clears this
    # field back to null. Lets a plan's validity count from first use
    # instead of from creation/purchase time (e.g. "30-day plan starting
    # whenever the customer actually connects").
    expire_days_after_first_use = Column(Integer, nullable=True)

    # Combined cap on concurrent RADIUS (OpenVPN/L2TP) sessions across ALL
    # of this user's connections put together - e.g. a user with 3 servers
    # bundled from a package and max_concurrent_sessions=1 can only be
    # logged into ONE of them at a time in total, not one on each. NULL =
    # no user-level cap (falls back to each Connection's own
    # max_concurrent_sessions, checked independently per connection - the
    # old behavior, still used for manually-added connections).
    max_concurrent_sessions = Column(Integer, nullable=True)

    # Flags so the daily "نزدیک به اتمام" reminder job (services/notify.py)
    # sends each warning at most once per occurrence instead of spamming the
    # customer every single day it stays true. Both get reset back to False
    # whenever the thing that made them true stops being true (quota reset/
    # topped-up, or expiry pushed back out past the 3-day window) so a
    # renewed user gets warned again next time they approach the edge.
    notified_quota_80 = Column(Boolean, default=False)
    notified_expiry_soon = Column(Boolean, default=False)

    # ---------------------------------------------------------------------
    # Referral program (کد دعوت) - see PanelSettings.referral_* for the
    # reward amounts. Every user gets their own short code (generated once,
    # at creation - see services/user_ops.py's _generate_referral_code) they
    # can share; `referred_by_id` is set at most ONCE, only if this user was
    # created by entering somebody ELSE's code during their first purchase
    # (see telegram_bot/handlers/admin_pending.py + routers/bot.py's
    # apply_referral) - never retroactively changed afterward.
    # `referral_reward_granted` guards against ever double-granting the
    # referrer's reward for the same referred user (e.g. a retried/duplicate
    # apply_referral call), independent of PanelSettings' reward amounts
    # changing later.
    referral_code = Column(String(16), unique=True, index=True, nullable=True)
    referred_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    referral_reward_granted = Column(Boolean, default=False)

    # ---------------------------------------------------------------------
    # Loyalty rewards (کاربر وفادار) - independent of the referral program.
    # purchase_count increments on every successful new-purchase/renewal
    # (see services/user_ops.py); loyalty_rewards_given tracks how many
    # threshold-crossings have already been rewarded so
    # `purchase_count // PanelSettings.loyalty_purchase_threshold >
    # loyalty_rewards_given` is the exact "is a new reward due" check -
    # comparing counts instead of a simple "% == 0" so a jump of more than
    # one threshold at once (e.g. a bulk admin action) still only grants the
    # reward it hasn't already gotten, not one per crossed multiple.
    purchase_count = Column(Integer, nullable=False, default=0)
    loyalty_rewards_given = Column(Integer, nullable=False, default=0)

    # ---------------------------------------------------------------------
    # Queued/reserved renewal (بسته رزرو): when renew_user() is called
    # while the user's CURRENT quota and expiry both still have room left,
    # the new gb/days/package aren't applied right away - they're stashed
    # here instead, and only actually take effect (full reset: usage=0,
    # fresh quota/expiry from these values) once the current package
    # actually runs out (quota exhausted OR expired) - see
    # services/user_ops.py's renew_user/_maybe_activate_reserved_renewal,
    # called from quota_manager.py's _enforce_user_limits (the single
    # choke point both the poll loop and RADIUS accounting funnel through)
    # and radius_server.py's HandleAuthPacket (so a login attempt right at
    # the exhaustion boundary doesn't get needlessly rejected for up to one
    # poll cycle). NULL/0 in both gb/days columns == nothing reserved.
    reserved_quota_bytes = Column(BigInteger, nullable=True)
    reserved_duration_days = Column(Integer, nullable=True)
    reserved_package_id = Column(Integer, ForeignKey("packages.id"), nullable=True)
    reserved_created_at = Column(DateTime, nullable=True)  # when this reservation was queued - shown to the customer/admin

    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)

    connections = relationship(
        "Connection", back_populates="user", cascade="all, delete-orphan"
    )
    owner_admin = relationship("AdminUser")
    # foreign_keys is required here - reserved_package_id (added for the
    # "reserved renewal" feature above) is a SECOND foreign key from User to
    # Package, so without this SQLAlchemy can't tell which column this
    # relationship should join on (AmbiguousForeignKeysError at startup).
    package = relationship("Package", foreign_keys=[package_id])
    reserved_package = relationship("Package", foreign_keys=[reserved_package_id])
    referred_by = relationship("User", remote_side="User.id")

    @property
    def remaining_bytes(self):
        if not self.total_quota_bytes:
            return None
        return max(self.total_quota_bytes - self.used_bytes, 0)

    @property
    def owner_admin_username(self) -> Optional[str]:
        return self.owner_admin.username if self.owner_admin else None


class Connection(Base):
    """One protocol-specific credential belonging to a user, living on a
    specific node. A user may have several (e.g. one WireGuard peer + one
    Xray VLESS account) and traffic from any of them deducts from the same
    shared user quota."""

    __tablename__ = "connections"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    node_id = Column(Integer, ForeignKey("nodes.id"), nullable=False, index=True)
    type = Column(Enum(ConnectionType), nullable=False)
    enabled = Column(Boolean, default=True)

    # WireGuard specific
    wg_peer_name = Column(String(128), nullable=True, index=True)
    wg_public_key = Column(String(128), nullable=True)
    wg_private_key = Column(String(128), nullable=True)  # shown once to the user
    wg_client_address = Column(String(64), nullable=True)  # e.g. 10.66.66.5/32

    # OpenVPN / L2TP specific (RouterOS PPP secret / RADIUS credential)
    ppp_username = Column(String(128), nullable=True, index=True)
    ppp_password = Column(String(128), nullable=True)

    # Xray specific
    xr_uuid = Column(String(64), nullable=True)
    xr_email = Column(String(255), nullable=True, index=True)  # unique tag/email used in config
    xr_flow = Column(String(64), nullable=True, default="")

    # RADIUS accounting session id currently open for this connection
    # (openvpn/l2tp only) - used to detect a new session vs. an
    # Interim-Update/Stop for the one we already have a baseline for.
    radius_session_id = Column(String(128), nullable=True)

    # Max simultaneous RADIUS sessions allowed for this credential (mirrors
    # MikroTik User Manager's "shared-users"). 0/None = unlimited. Only
    # meaningful for openvpn/l2tp (enforced by the RADIUS auth handler).
    max_concurrent_sessions = Column(Integer, nullable=True, default=1)

    # Set by the RADIUS auth handler after repeated over-the-limit connection
    # attempts; while in the future, ALL auth attempts for this connection
    # are rejected regardless of correct credentials or free session slots.
    banned_until = Column(DateTime, nullable=True)

    # Counters as last seen on the remote node - used to compute deltas
    last_rx_bytes = Column(BigInteger, default=0)
    last_tx_bytes = Column(BigInteger, default=0)

    total_bytes = Column(BigInteger, default=0)  # lifetime total for this connection only

    # Live "is this client currently connected" flag - for openvpn/l2tp this
    # is derived on read from RadiusActiveSession (real-time via RADIUS
    # accounting) instead, never from this column. For the other two
    # protocol types, which have no such live push mechanism, this column
    # is refreshed once per poll cycle: xray/vless from the node's own
    # online-clients API (3X-UI only for now - see
    # ThreeXUIClient.get_online_emails, poll_xray_node), and wireguard from
    # how recently the peer last handshook on the router (poll_mikrotik_node,
    # see quota_manager.WIREGUARD_ONLINE_THRESHOLD_SECONDS). Used together
    # with RadiusActiveSession to build the user-level concurrent-session
    # count across ALL of a user's services - see radius_server.py.
    online = Column(Boolean, default=False)

    created_at = Column(DateTime, default=now)

    # Groups connections that were provisioned together as ONE purchase
    # (e.g. all the services bundled into a package, created in a single
    # request) so the bot's "اکانت من" screen can show one button per
    # purchase instead of one flat button per connection - see
    # services/user_ops.py's provision_package_connections/bulk_create_users
    # (which generate one uuid4().hex per purchase and stamp it on every
    # connection created in that call) and
    # telegram_bot/keyboards.group_connections_by_purchase. NULL means this
    # connection was added on its own (manual "add connection", or data from
    # before this feature existed) - it just becomes its own single-item
    # group, same as before.
    purchase_batch = Column(String(40), nullable=True, index=True)
    # Package name at the moment of purchase, snapshotted here (rather than
    # a live FK to Package) purely for display in that grouped view, since
    # the package can be renamed/deleted later without that breaking old
    # purchase history. NULL for connections not created from a package.
    package_name_snapshot = Column(String(255), nullable=True)

    user = relationship("User", back_populates="connections")
    node = relationship("Node", back_populates="connections")


class UsageLog(Base):
    """Periodic snapshot of usage deltas, used to draw charts."""

    __tablename__ = "usage_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    connection_id = Column(Integer, ForeignKey("connections.id"), nullable=True, index=True)
    delta_bytes = Column(BigInteger, default=0)
    created_at = Column(DateTime, default=now, index=True)


class RadiusActiveSession(Base):
    """One currently-open RADIUS session (Acct-Start received, no Acct-Stop
    yet) for a connection - used only to enforce
    Connection.max_concurrent_sessions. Rows are created on Start, refreshed
    on Interim-Update, and removed on Stop; a periodic cleanup job also
    prunes rows that haven't been refreshed in a while, in case a Stop
    packet is ever lost (router reboot, network blip, etc.)."""

    __tablename__ = "radius_active_sessions"

    id = Column(Integer, primary_key=True, index=True)
    connection_id = Column(Integer, ForeignKey("connections.id"), nullable=False, index=True)
    session_id = Column(String(128), nullable=False)
    nas_ip = Column(String(64), nullable=True)
    started_at = Column(DateTime, default=now)
    last_seen_at = Column(DateTime, default=now, index=True)


class RadiusLimitEventLog(Base):
    """Persisted history of RADIUS auth attempts rejected for exceeding the
    concurrent-session limit (Connection.max_concurrent_sessions /
    User.max_concurrent_sessions), and of the temporary bans that follow
    repeated attempts - see services/radius_server.py's HandleAuthPacket
    (the `limit and active_count >= limit` branch) which is the only writer
    of this table, and _record_overlimit_attempt for the ban threshold
    itself. Before this table existed, these events only ever showed up in
    the container's own `logger.info` output (docker logs), invisible from
    the panel itself - an admin had to SSH in and grep to see who got
    banned and when. user_id/username/connection_type are denormalized
    (copied at write time rather than only living behind a join) so this
    history stays meaningful even if the user/connection is later renamed
    or deleted."""

    __tablename__ = "radius_limit_event_logs"

    id = Column(Integer, primary_key=True, index=True)
    connection_id = Column(Integer, ForeignKey("connections.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    owner_admin_id = Column(Integer, ForeignKey("admin_users.id"), nullable=True, index=True)
    username = Column(String(128), nullable=True, index=True)
    connection_type = Column(String(32), nullable=True)
    # "reject": this single attempt was rejected for being over the limit.
    # "ban": this attempt ALSO just triggered a new temporary ban (i.e. the
    # Nth reject within the abuse-detection window - see BAN_DURATION_MINUTES/
    # OVERLIMIT_ATTEMPTS_THRESHOLD in radius_server.py).
    event_type = Column(String(16), nullable=False, default="reject")
    active_count = Column(Integer, nullable=True)
    limit_value = Column(Integer, nullable=True)
    banned_until = Column(DateTime, nullable=True)  # set only for event_type="ban"
    # Best-effort caller IP for this rejected attempt - Calling-Station-Id
    # (RADIUS attribute 31) if the NAS (MikroTik) sent one, which for
    # PPP-based protocols (L2TP/PPTP/OpenVPN/SSTP) is normally the client's
    # real remote IP; falls back to the NAS's own IP (the router that
    # forwarded the request) if the NAS didn't send Calling-Station-Id at
    # all - see services/radius_server.py's HandleAuthPacket. Lets an admin
    # tell "same device retrying" from "account being shared across
    # different IPs" apart at a glance instead of only ever seeing the
    # username repeated.
    client_ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=now, index=True)


class Package(Base):
    """A purchasable plan (quota + duration + price) shown to customers by
    the sales bot. Optionally bundles one or more server+protocol combos
    (see PackageConnection) that get provisioned automatically for the
    user the moment the package is picked - either from the "ساخت کاربر"
    form in the web panel or (once wired up) the sales bot - instead of
    picking a node/protocol by hand every time."""

    __tablename__ = "packages"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    quota_gb = Column(Float, default=0)  # 0 = unlimited
    duration_days = Column(Integer, default=30)  # 0/None = never expires
    price = Column(BigInteger, default=0)  # in tomans, charged to customers
    # Wholesale price charged to a non-superadmin ADMIN's own credit balance
    # (see AdminUser.balance) when they create a user with this package for
    # their own group - NULL means "same as price" (no discount configured).
    # Never applies to a superadmin, who is never charged for anything.
    cooperation_price = Column(BigInteger, nullable=True)
    description = Column(Text, nullable=True)
    # `enabled` controls visibility in the WEB PANEL only (create-user /
    # renew-quick-action package dropdowns - see frontend Users.jsx's
    # `.filter(p => p.enabled)`). `bot_enabled` is the same idea but for the
    # Telegram sales bot's package picker (routers/bot.py's list_packages)
    # - split into two independent flags because an admin may want a
    # package sold only through one channel (e.g. a "manual/negotiated"
    # package an admin assigns from the panel but that shouldn't show up
    # as a self-serve bot purchase option, or vice versa).
    enabled = Column(Boolean, default=True)
    bot_enabled = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=now)

    # Combined concurrent-session cap applied to the WHOLE package (copied
    # onto User.max_concurrent_sessions when a user is created with this
    # package) - covers every bundled OpenVPN/L2TP service together, e.g. a
    # 4-server package with this set to 1 still only allows one active
    # session at a time in total, not one per server. NULL = unlimited.
    max_concurrent_sessions = Column(Integer, nullable=True)

    # Free-text message the sales bot sends to the customer right after a
    # successful purchase/renewal of this package (e.g. setup instructions,
    # support contact) - in addition to the connection links it already
    # sends. Optional; null/empty sends nothing extra.
    custom_message = Column(Text, nullable=True)

    connections = relationship(
        "PackageConnection", back_populates="package", cascade="all, delete-orphan"
    )
    files = relationship(
        "PackageFile", back_populates="package", cascade="all, delete-orphan"
    )


class PackageFile(Base):
    """A file (VPN client config, setup guide, installer APK, etc.) attached
    to a Package in the admin panel, that the sales bot sends to the
    customer automatically right after they buy/renew this package.
    Uploaded via the web panel; stored on disk under the same persistent
    /app/data volume the database itself lives on (stored_path is an
    absolute in-container path, never exposed to the web frontend or any
    HTTP response - only the built-in bot reads it directly, from the same
    filesystem, to hand the file to aiogram)."""

    __tablename__ = "package_files"

    id = Column(Integer, primary_key=True, index=True)
    package_id = Column(Integer, ForeignKey("packages.id"), nullable=False, index=True)
    filename = Column(String(255), nullable=False)  # original name shown to the customer
    stored_path = Column(String(500), nullable=False)
    content_type = Column(String(128), nullable=True)
    size_bytes = Column(BigInteger, default=0)
    created_at = Column(DateTime, default=now)

    package = relationship("Package", back_populates="files")


class PackageConnection(Base):
    """One server+protocol combo bundled into a Package. Selecting the
    package provisions all of these for the user in one go. The
    concurrent-session cap lives on the Package itself (applies across all
    bundled services combined), not per row here."""

    __tablename__ = "package_connections"

    id = Column(Integer, primary_key=True)
    package_id = Column(Integer, ForeignKey("packages.id"), nullable=False, index=True)
    node_id = Column(Integer, ForeignKey("nodes.id"), nullable=False, index=True)
    protocol = Column(Enum(ConnectionType), nullable=False)
    flow = Column(String(64), nullable=True)  # xray only

    package = relationship("Package", back_populates="connections")
    node = relationship("Node")


class DiscountCode(Base):
    """Admin-managed promo code (separate feature from the referral
    program - see User.referral_code) a customer can type in at checkout in
    the bot to knock money off a package's price. `value` is interpreted
    according to `kind`: a percentage (0-100) or a flat toman amount -
    never both, `kind` picks which. `max_uses` caps TOTAL redemptions
    across every customer (NULL = unlimited); per-customer reuse is
    prevented separately by DiscountCodeRedemption (one redemption row per
    user per code, checked before accepting a code a second time from the
    same account)."""

    __tablename__ = "discount_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(64), unique=True, index=True, nullable=False)
    kind = Column(String(16), nullable=False, default="percent")  # "percent" or "fixed"
    value = Column(Float, nullable=False, default=0)  # percent (0-100) or toman amount, per `kind`
    max_uses = Column(Integer, nullable=True)  # NULL = unlimited total redemptions
    used_count = Column(Integer, nullable=False, default=0)
    enabled = Column(Boolean, default=True)
    expires_at = Column(DateTime, nullable=True)  # NULL = never expires
    note = Column(String(255), nullable=True)  # admin-only label, e.g. "کمپین نوروز"
    created_at = Column(DateTime, default=now)

    redemptions = relationship("DiscountCodeRedemption", back_populates="code", cascade="all, delete-orphan")


class DiscountCodeRedemption(Base):
    """One row per successful use of a DiscountCode by a specific user -
    both the running audit trail shown in the panel and the mechanism that
    enforces "each customer can use a given code at most once" (checked via
    a lookup here before accepting the code, rather than a UNIQUE
    constraint, so admins can see WHEN/on-what-order it was used)."""

    __tablename__ = "discount_code_redemptions"

    id = Column(Integer, primary_key=True, index=True)
    code_id = Column(Integer, ForeignKey("discount_codes.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    username = Column(String(128), nullable=True)  # denormalized, survives the user later being deleted
    package_price = Column(BigInteger, nullable=True)  # price BEFORE discount, for the audit trail
    discount_amount = Column(BigInteger, nullable=True)  # actual toman amount knocked off
    created_at = Column(DateTime, default=now, index=True)

    code = relationship("DiscountCode", back_populates="redemptions")


class PanelSettings(Base):
    """Singleton row (always id=1) holding panel-wide settings that aren't
    tied to a specific node - currently just the card-to-card payment info
    the sales bot shows customers during checkout."""

    __tablename__ = "panel_settings"

    id = Column(Integer, primary_key=True)
    payment_card_number = Column(String(64), nullable=True)
    payment_card_holder = Column(String(128), nullable=True)
    payment_instructions = Column(Text, nullable=True)
    # Comma-separated toman amounts shown as quick-pick buttons in the bot's
    # "افزایش اعتبار" (top up credit) flow, e.g. "50000,100000,200000". The
    # customer can also always type a custom amount instead.
    topup_presets = Column(Text, nullable=True, default="")

    # ---------------------------------------------------------------------
    # HA / near-real-time replication به سرور دوم (مورد ۱۰). Off by default
    # (ha_enabled=False) - purely additive, zero behavior change unless the
    # admin explicitly turns it on. See services/backup.py's
    # create_snapshot_bytes/ha_pull_and_apply/ha_healthcheck and main.py's
    # ha_tick()/_promote_to_active() for the actual sync/failover logic;
    # routers/panel_settings.py's ha_router exposes the peer-facing
    # snapshot endpoint (X-API-Key auth, same header the external bot API
    # uses) plus the manual /resolve action below.
    ha_enabled = Column(Boolean, default=False)
    ha_mode = Column(String(20), nullable=True, default="standby")  # "primary" or "standby" - role THIS server plays
    ha_peer_url = Column(String(255), nullable=True)  # e.g. http://1.2.3.4:8000 - base URL of the OTHER server
    # An API key MINTED ON THE PEER (its own Settings > کلیدهای API page),
    # pasted here so THIS server can authenticate itself to the peer's
    # /api/ha/snapshot - never auto-generated/exchanged, always a manual
    # copy-paste step by the admin, same trust model as the external bot API.
    ha_peer_api_key = Column(String(128), nullable=True)
    # Split-brain guard: flips True once a standby auto-promotes itself
    # after losing contact with the peer. Once True, ha_tick() stops
    # pulling/overwriting this server's data from the peer entirely - even
    # if ha_mode is still nominally "standby" - until an admin manually
    # calls /api/ha/resolve after checking both servers by hand.
    ha_standby_active = Column(Boolean, default=False)
    ha_promoted_at = Column(DateTime, nullable=True)
    ha_last_sync_at = Column(DateTime, nullable=True)
    ha_last_health_ok_at = Column(DateTime, nullable=True)
    ha_last_error = Column(Text, nullable=True)

    # ---------------------------------------------------------------------
    # Configurable panel web port via Settings. The panel serves its UI
    # through the `frontend` container's nginx (see frontend/nginx.conf,
    # docker-compose.yml's "80:80" port mapping) - changing the HOST side of
    # that mapping means editing docker-compose.yml and re-running
    # `docker compose up -d` on the actual server. As of the local-docker-
    # socket rewrite this needs NO SSH/host/password at all any more - see
    # services/local_deploy.py's module docstring (docker.sock + project dir
    # bind-mounted into this container, see docker-compose.yml) - so this is
    # now genuinely just "the port" from the admin's point of view.
    panel_web_port = Column(Integer, nullable=True, default=80)  # last known/applied host-side port
    panel_port_status = Column(Text, nullable=True)  # last change attempt's result, shown in the UI
    panel_port_changed_at = Column(DateTime, nullable=True)
    # Legacy/unused columns from the old SSH-based version of this feature -
    # left in place (harmless) rather than dropped, to avoid an ALTER TABLE
    # DROP COLUMN migration for zero benefit. Nothing reads/writes these any
    # more.
    panel_ssh_host = Column(String(255), nullable=True)
    panel_ssh_port = Column(Integer, nullable=True, default=22)
    panel_ssh_username = Column(String(100), nullable=True, default="root")
    panel_project_dir = Column(String(255), nullable=True, default="/root/usermanager")

    # ---------------------------------------------------------------------
    # Static support contact shown by the bot's "🎧 پشتیبانی" menu button -
    # deliberately just admin-authored text (a username/phone/instructions),
    # not a ticketing system. Empty/NULL = button still shows but says
    # support info isn't configured yet, rather than hiding the button
    # entirely (simpler than threading "is this set?" into every menu build).
    support_contact_text = Column(Text, nullable=True)

    # ---------------------------------------------------------------------
    # Referral program (کد دعوت): every User gets a unique referral_code
    # (see User.referral_code below) they can share. The FIRST TIME a
    # brand-new customer's purchase is approved with a referral code
    # attached (see telegram_bot/handlers/admin_pending.py + routers/bot.py's
    # apply_referral), both sides get a one-time reward - amounts here, all
    # 0 by default (feature is a no-op until an admin sets at least one to a
    # positive value). Deliberately NOT itself an on/off toggle - all-zero
    # amounts already mean "nothing to give", so a separate enabled flag
    # would just be one more thing to forget to flip.
    referral_referrer_reward_credit = Column(BigInteger, nullable=False, default=0)  # تومان - to the person who referred
    referral_referrer_reward_gb = Column(Float, nullable=False, default=0)
    referral_new_user_reward_credit = Column(BigInteger, nullable=False, default=0)  # تومان - to the new signup
    referral_new_user_reward_gb = Column(Float, nullable=False, default=0)

    # ---------------------------------------------------------------------
    # Loyalty rewards (کاربر وفادار) - fully independent of the referral
    # program above: every `loyalty_purchase_threshold`-th successful
    # purchase/renewal by the SAME user automatically grants a reward, no
    # referral code involved at all. See User.purchase_count/
    # loyalty_rewards_given and services/user_ops.py's grant points.
    # NULL/0 threshold = feature disabled (default).
    loyalty_purchase_threshold = Column(Integer, nullable=True)
    loyalty_reward_credit = Column(BigInteger, nullable=False, default=0)  # تومان
    loyalty_reward_gb = Column(Float, nullable=False, default=0)


class BotSettings(Base):
    """Singleton row (always id=1) holding the built-in Telegram sales/admin
    bot's configuration. The bot runs in-process (see app/telegram_bot) so
    everything needed to run it - token, admin ids - is configured here from
    the web UI instead of an .env file / SSH."""

    __tablename__ = "bot_settings"

    id = Column(Integer, primary_key=True)
    bot_token = Column(String(255), nullable=True, default="")
    admin_ids = Column(Text, nullable=True, default="")  # comma-separated numeric telegram ids
    approval_chat_ids = Column(Text, nullable=True, default="")  # comma-separated, empty = same as admin_ids
    enabled = Column(Boolean, default=False)
    last_error = Column(Text, nullable=True)  # last startup/runtime error, shown in the UI
    updated_at = Column(DateTime, default=now, onupdate=now)

    # Remote deployment (see services/remote_deploy.py, routers/remote_bot.py):
    # lets the admin run the INTERACTIVE bot (getUpdates polling) on a
    # second server instead of in-process here, while it still talks to
    # this server's real database over `/api/bot/*` (X-API-Key). Telegram
    # only allows one poller per token, so `remote_mode=True` means the
    # local in-process bot is deliberately kept stopped (`enabled=False`)
    # while the remote one runs - only one of the two is ever actually
    # polling at a time. The SSH password is never stored - used once
    # during deploy/stop and discarded.
    remote_mode = Column(Boolean, default=False)
    remote_host = Column(String(255), nullable=True)
    remote_ssh_port = Column(Integer, nullable=True, default=22)
    remote_ssh_username = Column(String(100), nullable=True, default="root")
    remote_api_key_id = Column(Integer, ForeignKey("api_keys.id"), nullable=True)
    remote_status = Column(Text, nullable=True)  # last deploy/stop result, shown in the UI
    remote_deployed_at = Column(DateTime, nullable=True)

    # "Maintenance mode" for the customer-facing side of the bot only -
    # admins (both the global admin_ids list and linked group-admins, see
    # telegram_bot/admin_scope.py) can always use the bot regardless of
    # this flag. When False, every message/button tap from anyone else is
    # intercepted by telegram_bot/runner.py's MaintenanceModeMiddleware and
    # answered with a fixed "temporarily unavailable" message instead of
    # being routed to the normal customer handlers.
    customer_bot_enabled = Column(Boolean, default=True)

    # Per-item enable/disable for the customer main menu (see
    # telegram_bot/keyboards.py's main_menu_kb) - comma-separated action
    # keys (e.g. "cust_topup,cust_referral") that should be HIDDEN from the
    # menu. Empty/NULL = every item shown (default, zero behavior change).
    # Deliberately a single comma-separated column, same pattern as
    # admin_ids/approval_chat_ids above, instead of one boolean column per
    # item, to avoid a wide migration every time a menu item is added later.
    customer_menu_disabled_items = Column(Text, nullable=True, default="")


class Tutorial(Base):
    """An admin-authored help/tutorial entry (e.g. "نصب WireGuard روی
    اندروید") shown to customers from the bot's "📚 آموزش" menu - a title +
    free text + any number of attached photos/videos (see TutorialMedia)."""

    __tablename__ = "tutorials"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    text = Column(Text, nullable=True)
    enabled = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=now)

    media = relationship(
        "TutorialMedia", back_populates="tutorial", cascade="all, delete-orphan",
        order_by="TutorialMedia.id",
    )
    software = relationship(
        "TutorialSoftware", back_populates="tutorial", cascade="all, delete-orphan",
        order_by="TutorialSoftware.sort_order, TutorialSoftware.id",
    )


class TutorialMedia(Base):
    """One photo or video attached to a Tutorial. Stored on disk under the
    same persistent /app/data volume the database and package files use
    (see routers/tutorials.py) - stored_path is an absolute in-container
    path, not exposed to the web frontend (mirrors PackageFile)."""

    __tablename__ = "tutorial_media"

    id = Column(Integer, primary_key=True, index=True)
    tutorial_id = Column(Integer, ForeignKey("tutorials.id"), nullable=False, index=True)
    kind = Column(String(16), nullable=False)  # "photo" or "video"
    filename = Column(String(255), nullable=False)
    stored_path = Column(String(500), nullable=False)
    content_type = Column(String(128), nullable=True)
    size_bytes = Column(BigInteger, default=0)
    created_at = Column(DateTime, default=now)

    tutorial = relationship("Tutorial", back_populates="media")


class TutorialSoftware(Base):
    """One downloadable app/software entry attached to a Tutorial - shown
    as a "دانلود نرم‌افزار" section right alongside that tutorial's own
    content in the bot (see telegram_bot/handlers/tutorials.py), so a
    customer reading e.g. "نصب وایرگارد روی اندروید" also sees the actual
    WireGuard app to download without hunting for it elsewhere.

    Two independent ways to point at the software, either or both may be
    set: `url` (an external link - Google Play/App Store/official site,
    the common case) and/or an uploaded file (filename/stored_path, same
    on-disk pattern as PackageFile/TutorialMedia - the built-in bot sends
    it directly). At least one of the two should be set; the bot renders
    whichever are present."""

    __tablename__ = "tutorial_software"

    id = Column(Integer, primary_key=True, index=True)
    tutorial_id = Column(Integer, ForeignKey("tutorials.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)  # e.g. "وایرگارد - اندروید"
    url = Column(String(1000), nullable=True)
    filename = Column(String(255), nullable=True)
    stored_path = Column(String(500), nullable=True)
    content_type = Column(String(128), nullable=True)
    size_bytes = Column(BigInteger, default=0)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=now)

    tutorial = relationship("Tutorial", back_populates="software")
