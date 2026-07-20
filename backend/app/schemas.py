import datetime as dt
from typing import Optional, List

from pydantic import BaseModel, ConfigDict, field_validator

from .models import NodeType, ConnectionType, UserStatus

_PORT_FIELDS = (
    "mt_port", "mt_api_ssl_port", "mt_endpoint_port", "mt_ovpn_port", "mt_sstp_port",
    "xr_ssh_port", "xr_public_port",
)


# ---------- Auth ----------
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------- Node ----------
class NodeBase(BaseModel):
    name: str
    type: NodeType
    enabled: bool = True

    mt_host: Optional[str] = None
    mt_port: Optional[int] = 8728
    mt_api_ssl_port: Optional[int] = 8729
    mt_username: Optional[str] = None
    mt_password: Optional[str] = None
    mt_use_ssl: Optional[bool] = False
    mt_endpoint_host: Optional[str] = None

    mt_wireguard_interface: Optional[str] = "wireguard1"
    mt_endpoint_port: Optional[int] = 13231
    mt_client_dns: Optional[str] = "1.1.1.1"
    mt_client_subnet: Optional[str] = "10.66.66.0/24"

    mt_radius_secret: Optional[str] = None
    mt_ovpn_port: Optional[int] = 1194
    mt_ovpn_certificate: Optional[str] = None
    mt_l2tp_use_ipsec: Optional[bool] = True
    mt_l2tp_ipsec_secret: Optional[str] = None
    mt_ikev2_psk: Optional[str] = None
    mt_sstp_port: Optional[int] = 443
    mt_sstp_certificate: Optional[str] = None

    xr_panel_mode: Optional[str] = "ssh"  # "ssh" یا "3xui"
    xr_panel_base_url: Optional[str] = None
    xr_panel_api_token: Optional[str] = None
    xr_panel_username: Optional[str] = None
    xr_panel_password: Optional[str] = None
    xr_panel_inbound_id: Optional[int] = None

    xr_ssh_host: Optional[str] = None
    xr_ssh_port: Optional[int] = 22
    xr_ssh_username: Optional[str] = None
    xr_ssh_password: Optional[str] = None
    xr_ssh_private_key: Optional[str] = None
    xr_config_path: Optional[str] = "/usr/local/etc/xray/config.json"
    xr_service_name: Optional[str] = "xray"
    xr_api_address: Optional[str] = "127.0.0.1:10085"
    xr_inbound_tag: Optional[str] = "proxy"
    xr_public_host: Optional[str] = None
    xr_public_port: Optional[int] = 443
    xr_network: Optional[str] = "tcp"
    xr_security: Optional[str] = "tls"
    xr_sni: Optional[str] = None

    @field_validator(*_PORT_FIELDS)
    @classmethod
    def _validate_port(cls, v):
        if v is not None and not (1 <= v <= 65535):
            raise ValueError("شماره پورت باید بین 1 تا 65535 باشد")
        return v


class NodeCreate(NodeBase):
    pass


class NodeUpdate(NodeBase):
    name: Optional[str] = None
    type: Optional[NodeType] = None


class NodeOut(NodeBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    last_seen: Optional[dt.datetime] = None
    last_error: Optional[str] = None
    # NULL = superadmin-created/global infrastructure; an AdminUser id =
    # that level-2 Admin's own server (see models.Node.owner_admin_id).
    owner_admin_id: Optional[int] = None


# ---------- RADIUS auto-provisioning ----------
class RadiusPushRequest(BaseModel):
    # IP/host of THIS user-manager server, as reachable from the router.
    # Optional - falls back to the PANEL_PUBLIC_HOST env var if not given.
    panel_host: Optional[str] = None
    # How often RouterOS sends Interim-Update accounting packets, as
    # HH:MM:SS. Shorter = more near-live usage numbers in the panel, at the
    # cost of a bit more RADIUS/DB traffic. Default matches RouterOS's own
    # default of 5 minutes; 1 minute is a reasonable "near-live" setting.
    interim_update: Optional[str] = "00:05:00"


class RadiusPushResult(BaseModel):
    ok: bool
    message: str


# ---------- SSTP/L2TP/IKEv2 auto-provisioning ----------
# All three share the same "panel_host" fallback-to-PANEL_PUBLIC_HOST idea
# as RadiusPushRequest above, since SSTP/IKEv2 also need to register this
# panel as a RADIUS client on the router (SSTP via the existing service=ppp
# entry that push-radius-config already sets up; IKEv2 via a separate
# service=ipsec entry pushed by this same endpoint).
class ProtocolPushRequest(BaseModel):
    panel_host: Optional[str] = None


class ProtocolPushResult(BaseModel):
    ok: bool
    message: str


# ---------- Import pre-existing /ppp/secret accounts ----------
class PppImportSkipped(BaseModel):
    name: str
    reason: str


class PppImportResult(BaseModel):
    imported: List[str]
    imported_count: int
    skipped: List[PppImportSkipped]
    skipped_count: int


# ---------- Connection ----------
class ConnectionCreateWireguard(BaseModel):
    node_id: int
    # >1 means this one WireGuard peer/config is meant to be shared by
    # several people at once (see services/user_ops.py's provision_wireguard
    # docstring) - reserves that many adjacent IPs for the SAME peer instead
    # of creating a separate config per person.
    max_concurrent_sessions: Optional[int] = 1


class ConnectionCreateOpenvpn(BaseModel):
    node_id: int
    max_concurrent_sessions: Optional[int] = 1  # 0 = unlimited


class ConnectionCreateL2tp(BaseModel):
    node_id: int
    max_concurrent_sessions: Optional[int] = 1  # 0 = unlimited


class ConnectionCreateIkev2(BaseModel):
    node_id: int
    max_concurrent_sessions: Optional[int] = 1  # 0 = unlimited


class ConnectionCreateSstp(BaseModel):
    node_id: int
    max_concurrent_sessions: Optional[int] = 1  # 0 = unlimited


class ConnectionCreateXray(BaseModel):
    node_id: int
    flow: Optional[str] = ""


class ApplyPackageRequest(BaseModel):
    # Applies an EXISTING package's bundled services to a user who already
    # exists (as opposed to package_id on UserCreate, which only applies at
    # creation time) - see routers/users.py's apply_package. Used for both
    # "this user has no package/services yet, give them one" and "give this
    # user an ADDITIONAL package on top of what they already have" - the new
    # services land in their own real, independently-enforced Purchase (see
    # models.Purchase's docstring) with its OWN quota/usage/expiry, so it
    # never rides along under (or gets silently swallowed by) whatever quota
    # the user already had from before.
    package_id: int


class ConnectionUpdate(BaseModel):
    enabled: Optional[bool] = None
    max_concurrent_sessions: Optional[int] = None
    banned_until: Optional[dt.datetime] = None  # set to null/past to unban
    # Editable connection identity/credentials (see routers/users.py's
    # update_connection) - only the field(s) relevant to the connection's
    # own type should be sent; wg_peer_name is also synced to the MikroTik
    # peer's comment, ppp_username/ppp_password are DB-only (RADIUS reads
    # straight from here, see services/radius_server.py).
    wg_peer_name: Optional[str] = None
    ppp_username: Optional[str] = None
    ppp_password: Optional[str] = None


class PurchaseOut(BaseModel):
    """A single independently-enforced package purchase - see
    models.Purchase's docstring. Only ever present for connections added via
    the "افزودن پکیج" (apply-package) flow; every other connection has
    purchase_id=None and is governed by the owning User's own combined
    quota fields instead."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    package_id: Optional[int] = None
    package_name_snapshot: Optional[str] = None
    quota_bytes: int = 0  # 0 == unlimited
    used_bytes: int = 0
    remaining_bytes: Optional[int] = None
    expire_at: Optional[dt.datetime] = None
    expire_days_after_first_use: Optional[int] = None
    max_concurrent_sessions: Optional[int] = None
    status: UserStatus
    reserved_quota_bytes: Optional[int] = None
    reserved_duration_days: Optional[int] = None
    reserved_package_id: Optional[int] = None
    reserved_created_at: Optional[dt.datetime] = None
    created_at: dt.datetime


class PurchaseRenewRequest(BaseModel):
    add_gb: float = 0
    add_days: int = 0
    reset_usage: bool = False
    # Optional - lets a renewal also switch this purchase to a different
    # package's quota/duration instead of just adding gb/days to the
    # current one (mirrors user_ops.renew_user's package_id parameter).
    package_id: Optional[int] = None


class ConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    node_id: int
    type: ConnectionType
    enabled: bool
    wg_peer_name: Optional[str] = None
    wg_public_key: Optional[str] = None
    wg_private_key: Optional[str] = None
    wg_client_address: Optional[str] = None
    ppp_username: Optional[str] = None
    ppp_password: Optional[str] = None
    xr_uuid: Optional[str] = None
    xr_email: Optional[str] = None
    xr_flow: Optional[str] = None
    radius_session_id: Optional[str] = None
    max_concurrent_sessions: Optional[int] = None
    banned_until: Optional[dt.datetime] = None
    active_session_count: int = 0
    # Live connected/not-connected flag. For openvpn/l2tp this is
    # overridden by the router from RadiusActiveSession (real-time); for
    # xray it comes straight from this column, refreshed once per poll
    # cycle from the node's online-clients API (3X-UI only - see
    # ThreeXUIClient.get_online_emails).
    online: bool = False
    # Client's live remote IP, when known - openvpn/l2tp/ikev2/sstp get this
    # from the open RadiusActiveSession's client_ip (routers/users.py fills
    # it in next to `online` for these types); wireguard comes straight from
    # last_client_ip below (refreshed each poll from the router's peer
    # endpoint - see quota_manager.poll_mikrotik_node); xray has no source
    # for this and stays None. Shown next to the آنلاین badge in
    # UserDetail.jsx.
    client_ip: Optional[str] = None
    last_client_ip: Optional[str] = None
    last_rx_bytes: int = 0
    last_tx_bytes: int = 0
    total_bytes: int = 0
    created_at: dt.datetime
    # See models.Connection.purchase_batch/package_name_snapshot - used to
    # group a user's connections by purchase in the bot's "اکانت من" screen.
    purchase_batch: Optional[str] = None
    package_name_snapshot: Optional[str] = None
    # See models.Connection.purchase_id/models.Purchase - set only for
    # connections added via "افزودن پکیج", where quota/expiry is tracked
    # independently instead of through the owning user's combined fields.
    purchase_id: Optional[int] = None


class ConnectionShareLink(BaseModel):
    connection_id: int
    kind: str
    link: Optional[str] = None
    config_text: Optional[str] = None


# ---------- User ----------
class UserBase(BaseModel):
    username: str
    full_name: Optional[str] = None
    notes: Optional[str] = None
    total_quota_bytes: int = 0
    expire_at: Optional[dt.datetime] = None
    expire_days_after_first_use: Optional[int] = None
    telegram_id: Optional[int] = None
    max_concurrent_sessions: Optional[int] = None


class UserCreate(UserBase):
    # If set, quota/expiry are taken from the package (overriding whatever
    # was in total_quota_bytes/expire_at above) and every server/service
    # bundled into the package is provisioned automatically. Also stamped
    # onto the created user's own package_id (see models.User) so it can
    # later be filtered/selected by package in the users list.
    package_id: Optional[int] = None
    # Only superadmins may set this (which admin/group owns the new user -
    # see routers/users.py); ignored from non-superadmin requests, who
    # always get owner_admin_id = themselves regardless of what they send.
    owner_admin_id: Optional[int] = None


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    notes: Optional[str] = None
    total_quota_bytes: Optional[int] = None
    expire_at: Optional[dt.datetime] = None
    expire_days_after_first_use: Optional[int] = None
    # If provided, clear_expire_days_trigger=True clears
    # expire_days_after_first_use (used when the admin switches back to a
    # fixed date/no-expiry after having set a "count from first use" plan).
    clear_expire_days_trigger: Optional[bool] = None
    status: Optional[UserStatus] = None
    telegram_id: Optional[int] = None
    balance: Optional[int] = None
    # Combined cap across ALL of this user's openvpn/l2tp connections put
    # together (a real column on User - see models.py for why this isn't
    # per-connection anymore).
    max_concurrent_sessions: Optional[int] = None
    # Reassign this user to a different admin/group - only a superadmin's
    # request is allowed to change this (enforced in routers/users.py, not
    # here); silently ignored from a non-superadmin's update request.
    owner_admin_id: Optional[int] = None


class UserOut(UserBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    used_bytes: int
    balance: int = 0
    status: UserStatus
    created_at: dt.datetime
    updated_at: dt.datetime
    connections: List[ConnectionOut] = []
    owner_admin_id: Optional[int] = None
    # Convenience for the panel's user list/detail - filled in by the
    # router (not a real relationship traversal here) so the UI doesn't
    # need a second request just to show "کدام ادمین".
    owner_admin_username: Optional[str] = None
    package_id: Optional[int] = None
    # Referral program (کد دعوت) - see models.User/PanelSettings
    referral_code: Optional[str] = None
    purchase_count: int = 0
    # Queued renewal (بسته رزرو) - see models.User.reserved_quota_bytes's
    # docstring. Non-null gb/days means this user paid for a renewal that
    # hasn't taken effect yet because their current package still has room.
    reserved_quota_bytes: Optional[int] = None
    reserved_duration_days: Optional[int] = None
    reserved_package_id: Optional[int] = None
    reserved_created_at: Optional[dt.datetime] = None
    # Independently-tracked package purchases added via "افزودن پکیج" - see
    # models.Purchase. Empty for users who have never used that feature.
    purchases: List[PurchaseOut] = []


class UserListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    full_name: Optional[str] = None
    total_quota_bytes: int
    used_bytes: int
    status: UserStatus
    expire_at: Optional[dt.datetime] = None
    # If set (and expire_at is still null), this user's expiry hasn't
    # started yet - it activates on their first successful RADIUS login.
    # The users list needs this to show "pending activation" instead of
    # misleadingly showing "بدون انقضا" (truly unlimited) for these.
    expire_days_after_first_use: Optional[int] = None
    connections_count: int = 0
    # True if the user has at least one currently-connected session, PPP
    # (RadiusActiveSession) or xray (Connection.online) alike - computed by
    # the router, not read straight off the User row.
    online: bool = False
    owner_admin_id: Optional[int] = None
    owner_admin_username: Optional[str] = None
    package_id: Optional[int] = None


class UserListPage(BaseModel):
    items: List[UserListItem]
    total: int
    page: int
    page_size: int


# ---------- Bulk user operations ----------
class BulkConnectionSpec(BaseModel):
    node_id: int
    protocol: ConnectionType
    max_concurrent_sessions: Optional[int] = 1


class BulkCreateUsersRequest(BaseModel):
    prefix: str
    count: int
    package_id: Optional[int] = None  # when set, quota/expiry/connections all come from the package instead
    quota_gb: float = 0
    expire_days: Optional[int] = None
    notes: Optional[str] = None
    connections: List[BulkConnectionSpec] = []


class BulkOpSkipped(BaseModel):
    name: str
    reason: str


class BulkCreateUsersResult(BaseModel):
    created: List[str]
    created_count: int
    skipped: List[BulkOpSkipped]
    skipped_count: int


class BulkUpdateUsersRequest(BaseModel):
    user_ids: List[int]
    add_gb: float = 0
    add_days: int = 0
    reset_usage: bool = False
    status: Optional[UserStatus] = None
    max_concurrent_sessions: Optional[int] = None  # applied to all of the user's connections
    # If set, every selected user's quota/duration/concurrent-session-cap is
    # overwritten outright from this package (like a fresh "renew with
    # package" for each of them) and their package_id is stamped to match -
    # takes priority over add_gb/add_days above when both are sent, so a
    # group can be re-based onto a different package plan in one action.
    package_id: Optional[int] = None

    @field_validator("user_ids")
    @classmethod
    def _cap_user_ids(cls, v):
        if len(v) > 1000:
            raise ValueError("حداکثر ۱۰۰۰ کاربر در هر بار")
        return v


class BulkUpdateUsersResult(BaseModel):
    updated_count: int


class BulkDeleteUsersRequest(BaseModel):
    user_ids: List[int]

    @field_validator("user_ids")
    @classmethod
    def _cap_user_ids(cls, v):
        if len(v) > 1000:
            raise ValueError("حداکثر ۱۰۰۰ کاربر در هر بار")
        return v


class BulkDeleteUsersResult(BaseModel):
    deleted_count: int


# ---------- Dashboard ----------
class DashboardStats(BaseModel):
    total_users: int
    active_users: int
    disabled_users: int
    quota_exceeded_users: int
    total_nodes: int
    online_nodes: int
    online_users_now: int
    total_used_bytes: int
    total_quota_bytes: int
    usage_last_24h: List[dict]
    # Only set for a non-superadmin's own dashboard (see routers/dashboard.py) -
    # their current AdminUser.balance, so they can see at a glance how much
    # credit they have left to spend on packages. Null for superadmins
    # (unlimited/exempt from this charge - see _charge_admin_for_package).
    admin_balance: Optional[int] = None
    # Live-ish throughput: sum of UsageLog.delta_bytes recorded in the last
    # 60 seconds (across all connections in scope), divided by 60. Since
    # poll_all runs every POLL_INTERVAL_SECONDS (default 30s - see
    # config.py), this is effectively "average bytes/sec seen over the last
    # 1-2 poll cycles" - a rough live speed gauge, not a precise real-time
    # metric.
    avg_speed_bps: float = 0


# ---------- API keys (for the external/bot integration) ----------
class ApiKeyCreate(BaseModel):
    label: str


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    label: str
    key: str
    enabled: bool
    created_at: dt.datetime
    last_used_at: Optional[dt.datetime] = None


# ---------- Packages (purchasable plans, shown by the sales bot) ----------
class PackageConnectionSpec(BaseModel):
    node_id: int
    protocol: ConnectionType
    flow: Optional[str] = ""  # xray only


class PackageConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    node_id: int
    protocol: ConnectionType
    flow: Optional[str] = ""


class PackageFileOut(BaseModel):
    """Admin/bot-facing view of an uploaded package file. Deliberately has
    no path/URL field - the web panel only needs enough to list/delete
    files, and the built-in bot reads the real disk path straight from the
    DB itself (see telegram_bot/panel_bridge.py), never through this
    schema, so a raw server filesystem path never ends up in any HTTP
    response."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    filename: str
    size_bytes: int = 0
    created_at: dt.datetime


class PackageBase(BaseModel):
    name: str
    quota_gb: float = 0  # 0 = unlimited
    duration_days: Optional[int] = 30  # None/0 = never expires
    price: int = 0  # tomans, charged to customers
    # Wholesale price charged to a non-superadmin admin's own credit
    # balance when they create a user with this package - None = same as
    # `price` (no cooperation discount configured for this package).
    cooperation_price: Optional[int] = None
    description: Optional[str] = None
    enabled: bool = True  # visible in the web panel's package pickers
    bot_enabled: bool = True  # visible in the Telegram bot's package picker
    sort_order: int = 0
    # Combined cap across every bundled OpenVPN/L2TP service together (not
    # per service) - copied onto User.max_concurrent_sessions when a user
    # is created with this package. None/0 = unlimited.
    max_concurrent_sessions: Optional[int] = None
    # Sent by the sales bot to the customer right after a successful
    # purchase/renewal of this package, alongside any files attached below.
    custom_message: Optional[str] = None


class PackageCreate(PackageBase):
    connections: List[PackageConnectionSpec] = []


class PackageUpdate(BaseModel):
    name: Optional[str] = None
    quota_gb: Optional[float] = None
    duration_days: Optional[int] = None
    price: Optional[int] = None
    cooperation_price: Optional[int] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    bot_enabled: Optional[bool] = None
    sort_order: Optional[int] = None
    max_concurrent_sessions: Optional[int] = None
    custom_message: Optional[str] = None
    connections: Optional[List[PackageConnectionSpec]] = None


class PackageOut(PackageBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: dt.datetime
    connections: List[PackageConnectionOut] = []
    files: List[PackageFileOut] = []
    # Which level-2 Admin owns this package - None means superadmin-made/
    # global (see models.Package.owner_admin_id's docstring). Read-only,
    # always derived server-side from the creating admin, never accepted
    # from the client (not on PackageBase/PackageCreate).
    owner_admin_id: Optional[int] = None
    owner_admin_username: Optional[str] = None
    # This Seller's own custom resale price for this package (see
    # models.PackageSellerPrice) - None means they haven't set one, so
    # `price` above (the package's base price) is what applies. Only ever
    # populated when the CALLER is a level-3 Seller (routers/packages.py's
    # list_packages/_out); always None for a superadmin/Admin's own view,
    # since only Sellers layer a resale price on top of someone else's
    # package.
    my_price: Optional[int] = None


class SellerPackagePriceUpdate(BaseModel):
    # None/omitted = clear the override, fall back to the package's own
    # base price again.
    price: Optional[int] = None


# ---------- Discount codes (کد تخفیف) ----------
class DiscountCodeBase(BaseModel):
    code: str
    kind: str = "percent"  # "percent" (0-100) or "fixed" (toman amount)
    value: float = 0
    max_uses: Optional[int] = None
    enabled: bool = True
    expires_at: Optional[dt.datetime] = None
    note: Optional[str] = None

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, v):
        if v not in ("percent", "fixed"):
            raise ValueError("نوع تخفیف باید percent یا fixed باشد")
        return v


class DiscountCodeCreate(DiscountCodeBase):
    pass


class DiscountCodeUpdate(BaseModel):
    kind: Optional[str] = None
    value: Optional[float] = None
    max_uses: Optional[int] = None
    enabled: Optional[bool] = None
    expires_at: Optional[dt.datetime] = None
    note: Optional[str] = None


class DiscountCodeOut(DiscountCodeBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    used_count: int = 0
    created_at: dt.datetime


class DiscountCodeRedemptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code_id: int
    user_id: Optional[int] = None
    username: Optional[str] = None
    package_price: Optional[int] = None
    discount_amount: Optional[int] = None
    created_at: dt.datetime


class DiscountValidateRequest(BaseModel):
    code: str
    package_price: int = 0
    # Optional: lets validation also catch "you already used this code"
    # before the customer gets to the final confirm step - omitted for a
    # brand-new customer whose account doesn't exist yet (their first
    # purchase can never collide with a past redemption anyway).
    username: Optional[str] = None


class DiscountValidateResult(BaseModel):
    valid: bool
    reason: Optional[str] = None
    discount_amount: int = 0
    final_price: int = 0


class DiscountRedeemRequest(BaseModel):
    code: str
    username: str
    package_price: int = 0


class ReferralApplyRequest(BaseModel):
    username: str  # the brand-new user who was just created
    referral_code: str  # the code they entered


# ---------- Panel-wide settings (payment info shown by the sales bot) ----------
class PanelSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    payment_card_number: Optional[str] = None
    payment_card_holder: Optional[str] = None
    payment_instructions: Optional[str] = None
    topup_presets: Optional[str] = ""
    # HA / near-real-time replication به سرور دوم (مورد ۱۰) - see models.PanelSettings
    ha_enabled: bool = False
    ha_mode: Optional[str] = "standby"
    ha_peer_url: Optional[str] = None
    ha_peer_api_key: Optional[str] = None
    ha_standby_active: bool = False
    ha_promoted_at: Optional[dt.datetime] = None
    ha_last_sync_at: Optional[dt.datetime] = None
    ha_last_health_ok_at: Optional[dt.datetime] = None
    ha_last_error: Optional[str] = None
    # Configurable panel web port (see models.PanelSettings) - as of the
    # local-docker-socket rewrite this is genuinely just the one number;
    # panel_ssh_* fields still exist as legacy/unused DB columns (harmless,
    # left in place to avoid a DROP COLUMN migration) but are no longer
    # read/written anywhere in the app.
    panel_web_port: Optional[int] = 80
    panel_port_status: Optional[str] = None
    panel_port_changed_at: Optional[dt.datetime] = None
    # Support contact shown by the bot's "🎧 پشتیبانی" button (see models.PanelSettings)
    support_contact_text: Optional[str] = None
    # Referral program (کد دعوت) reward amounts - all 0 = feature is a no-op
    referral_referrer_reward_credit: int = 0
    referral_referrer_reward_gb: float = 0
    referral_new_user_reward_credit: int = 0
    referral_new_user_reward_gb: float = 0
    # Loyalty rewards (کاربر وفادار) - independent of referrals
    loyalty_purchase_threshold: Optional[int] = None
    loyalty_reward_credit: int = 0
    loyalty_reward_gb: float = 0


class PanelSettingsUpdate(BaseModel):
    payment_card_number: Optional[str] = None
    payment_card_holder: Optional[str] = None
    payment_instructions: Optional[str] = None
    topup_presets: Optional[str] = None
    ha_enabled: Optional[bool] = None
    ha_mode: Optional[str] = None
    ha_peer_url: Optional[str] = None
    ha_peer_api_key: Optional[str] = None
    support_contact_text: Optional[str] = None
    referral_referrer_reward_credit: Optional[int] = None
    referral_referrer_reward_gb: Optional[float] = None
    referral_new_user_reward_credit: Optional[int] = None
    referral_new_user_reward_gb: Optional[float] = None
    loyalty_purchase_threshold: Optional[int] = None
    loyalty_reward_credit: Optional[int] = None
    loyalty_reward_gb: Optional[float] = None


class PanelPortChangeRequest(BaseModel):
    # No SSH/host/password needed any more - the backend container talks to
    # the HOST's docker daemon directly over the mounted docker.sock (see
    # services/local_deploy.py) and edits docker-compose.yml through the
    # project directory bind-mounted at the same path. Just the new port.
    new_port: int

    @field_validator("new_port")
    @classmethod
    def _validate_new_port(cls, v):
        if not (1 <= v <= 65535):
            raise ValueError("شماره پورت باید بین 1 تا 65535 باشد")
        return v


class PanelPortChangeResult(BaseModel):
    ok: bool
    message: str


# ---------- Built-in Telegram bot settings (runs in-process, no .env/SSH) ----------
class BotSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    bot_token: Optional[str] = ""
    admin_ids: Optional[str] = ""
    approval_chat_ids: Optional[str] = ""
    enabled: bool = False
    last_error: Optional[str] = None
    running: bool = False
    bot_username: Optional[str] = None
    remote_mode: bool = False
    remote_host: Optional[str] = None
    remote_ssh_port: Optional[int] = 22
    remote_ssh_username: Optional[str] = "root"
    remote_status: Optional[str] = None
    remote_deployed_at: Optional[dt.datetime] = None
    customer_bot_enabled: bool = True
    customer_menu_disabled_items: Optional[str] = ""
    # See models.BotSettings.telegram_api_proxy_url's docstring - base URL
    # of a self-hosted reverse proxy to Telegram, applied to EVERY bot
    # instance (this shared one + every Admin/Seller's own bot), for a main
    # server with no direct outbound route to api.telegram.org.
    telegram_api_proxy_url: Optional[str] = None


class BotSettingsUpdate(BaseModel):
    bot_token: Optional[str] = None
    admin_ids: Optional[str] = None
    approval_chat_ids: Optional[str] = None
    enabled: Optional[bool] = None
    customer_bot_enabled: Optional[bool] = None
    customer_menu_disabled_items: Optional[str] = None
    telegram_api_proxy_url: Optional[str] = None


# ---------- Per-admin dedicated bot (3-tier hierarchy - see AdminUser.own_bot_token) ----------
class OwnBotSettingsOut(BaseModel):
    bot_token: Optional[str] = ""
    enabled: bool = True
    running: bool = False
    last_error: Optional[str] = None
    bot_username: Optional[str] = None
    # Whether this Admin has linked their own numeric Telegram id yet (see
    # AdminUser.telegram_id) - without it, their bot still runs for
    # customers but nobody gets the admin command menu on it.
    telegram_id_linked: bool = False


class OwnBotSettingsUpdate(BaseModel):
    bot_token: Optional[str] = None
    enabled: Optional[bool] = None


# ---------- Per-admin own card-to-card payment info (3-tier hierarchy - see
# AdminUser.own_payment_card_number and its docstring) ----------
class OwnPaymentSettingsOut(BaseModel):
    payment_card_number: Optional[str] = ""
    payment_card_holder: Optional[str] = ""
    payment_instructions: Optional[str] = ""
    topup_presets: Optional[str] = ""


class OwnPaymentSettingsUpdate(BaseModel):
    payment_card_number: Optional[str] = None
    payment_card_holder: Optional[str] = None
    payment_instructions: Optional[str] = None
    topup_presets: Optional[str] = None


# ---------- Remote bot deployment (install the interactive bot on a 2nd server) ----------
class RemoteBotDeployRequest(BaseModel):
    host: str
    ssh_port: int = 22
    ssh_username: str = "root"
    ssh_password: str  # used once for this deploy, never stored
    panel_public_url: Optional[str] = None  # e.g. http://1.2.3.4:8000 - how the REMOTE server reaches THIS one; defaults to this request's own host


class RemoteBotStopRequest(BaseModel):
    ssh_password: str  # re-entered on purpose - never persisted from the deploy call


class RemoteBotStatusOut(BaseModel):
    remote_mode: bool = False
    remote_host: Optional[str] = None
    remote_ssh_port: Optional[int] = 22
    remote_ssh_username: Optional[str] = "root"
    remote_status: Optional[str] = None
    remote_deployed_at: Optional[dt.datetime] = None


# ---------- Bot API (external customer-bot integration) ----------
class BotCreateConnectionSpec(BaseModel):
    node_id: int
    protocol: ConnectionType
    flow: Optional[str] = ""  # only used for xray
    # When set, groups this connection with others sharing the same value as
    # one "purchase" in the bot's "اکانت من" screen - see
    # models.Connection.purchase_batch. Only meaningful on the standalone
    # POST /users/{username}/connections endpoint (add_connection); ignored
    # elsewhere since BotCreateUserRequest.connections below all get ONE
    # batch generated automatically for the whole request.
    purchase_batch: Optional[str] = None
    package_name: Optional[str] = None


class BotCreateUserRequest(BaseModel):
    username: str
    full_name: Optional[str] = None
    quota_gb: float = 0  # 0 = unlimited
    expire_days: Optional[int] = None  # None = never expires
    telegram_id: Optional[int] = None
    connections: List[BotCreateConnectionSpec] = []
    # Purely a display label, snapshotted onto every connection created from
    # `connections` above (all sharing ONE auto-generated purchase batch) -
    # see models.Connection.package_name_snapshot.
    package_name: Optional[str] = None
    # Set by the built-in bot when a linked group-admin (not the global bot
    # admin list) creates this user - puts them straight into that admin's
    # group, same as creating them from the panel would. None (the sales
    # bot's own customer-signup flow never sends this) = unassigned.
    owner_admin_id: Optional[int] = None
    # The actual package id being purchased (package_name above is only a
    # display label) - stamped onto the created user's package_id so the
    # web panel can later filter/select this user by package (see
    # models.User.package_id). None for admin-created users with no package
    # (e.g. admin_users.py's manual node/protocol flow).
    package_id: Optional[int] = None


class BotRenewRequest(BaseModel):
    add_gb: float = 0
    add_days: int = 0
    reset_usage: bool = False
    # Same as BotCreateUserRequest.package_id above - if this renewal came
    # from a package purchase, stamp/refresh the user's package_id so
    # package-based filtering in the panel stays accurate across renewals
    # too (not just at first creation).
    package_id: Optional[int] = None


class BotAddBalanceRequest(BaseModel):
    amount: int  # tomans, can be negative to deduct


class BotLinkTelegramRequest(BaseModel):
    telegram_id: int


class BotConnectionInfo(BaseModel):
    id: int
    type: ConnectionType
    node_id: int
    node_name: str
    enabled: bool
    link: Optional[str] = None
    config_text: Optional[str] = None
    # Structured versions of what's already embedded in config_text/link -
    # lets the bot render its own clean, type-specific message (file+QR for
    # wireguard, plain fields for openvpn/l2tp, ...) instead of dumping the
    # human-readable Persian config_text blob into a chat message.
    server: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    psk: Optional[str] = None  # l2tp/ipsec pre-shared key, if configured
    # Lifetime bytes used by THIS connection alone (not the user's shared
    # total) - powers the bot's "مصرف هر سرویس" section.
    total_bytes: int = 0
    created_at: Optional[dt.datetime] = None
    # See models.Connection.purchase_batch/package_name_snapshot - lets the
    # bot group "اکانت من" by purchase instead of one flat list of services.
    purchase_batch: Optional[str] = None
    package_name: Optional[str] = None


class BotUserResponse(BaseModel):
    id: int
    username: str
    full_name: Optional[str] = None
    status: UserStatus
    total_quota_bytes: int
    used_bytes: int
    remaining_bytes: Optional[int] = None
    expire_at: Optional[dt.datetime] = None
    telegram_id: Optional[int] = None
    balance: int = 0
    connections: List[BotConnectionInfo] = []
    referral_code: Optional[str] = None
    # Transient, one-shot fields - only non-null on the exact response right
    # after a loyalty-threshold crossing (see services/user_ops.py's
    # _maybe_grant_loyalty_reward) so the bot can show a "🎁 loyalty reward!"
    # notice right after that purchase/renewal. Never re-sent afterward.
    loyalty_reward_credit: Optional[int] = None
    loyalty_reward_gb: Optional[float] = None
    # Queued renewal (بسته رزرو) - see models.User.reserved_quota_bytes's
    # docstring. Non-null means this customer already paid for a renewal
    # that's waiting for the current package to actually run out - shown in
    # "اکانت من" so it isn't mistaken for the renewal having failed/vanished.
    reserved_quota_gb: Optional[float] = None
    reserved_duration_days: Optional[int] = None


class BotUserListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    full_name: Optional[str] = None
    status: UserStatus
    total_quota_bytes: int
    used_bytes: int
    expire_at: Optional[dt.datetime] = None
    telegram_id: Optional[int] = None


class BotUserListPage(BaseModel):
    items: List[BotUserListItem]
    total: int
    page: int
    page_size: int


class BotNodeInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    type: NodeType


class BotAdminInfo(BaseModel):
    """Looked up by the built-in bot (GET /api/bot/admin-by-telegram/{id})
    to decide whether a Telegram user messaging it is a group-admin - see
    telegram_bot/admin_scope.py."""

    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    is_superadmin: bool


# ---------- Tutorials (bot "آموزش" section) ----------
class TutorialMediaOut(BaseModel):
    """Admin/bot-facing view of one attached photo/video. Like
    PackageFileOut, deliberately has no path field - the built-in bot reads
    the real disk path straight from the DB itself (see
    telegram_bot/panel_bridge.py), never through this schema."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str  # "photo" or "video"
    filename: str
    size_bytes: int = 0
    created_at: dt.datetime


class TutorialSoftwareOut(BaseModel):
    """Like TutorialMediaOut - no path field, the bot reads stored_path
    straight from the DB. The admin UI tells whether an uploaded file
    exists just by checking `filename` (separate from `url`, since either
    or both may be set - see models.TutorialSoftware's docstring)."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    url: Optional[str] = None
    filename: Optional[str] = None
    size_bytes: int = 0
    sort_order: int = 0
    created_at: dt.datetime


class TutorialSoftwareCreate(BaseModel):
    """URL-only creation (no file) - see the separate multipart upload
    endpoint for attaching a file instead/also."""
    name: str
    url: Optional[str] = None
    sort_order: int = 0


class TutorialBase(BaseModel):
    title: str
    text: Optional[str] = None
    enabled: bool = True
    sort_order: int = 0


class TutorialCreate(TutorialBase):
    pass


class TutorialUpdate(BaseModel):
    title: Optional[str] = None
    text: Optional[str] = None
    enabled: Optional[bool] = None
    sort_order: Optional[int] = None


class TutorialOut(TutorialBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: dt.datetime
    media: List[TutorialMediaOut] = []
    software: List[TutorialSoftwareOut] = []


# ---------- Admin management (superadmin only) ----------
class AdminCreate(BaseModel):
    username: str
    password: str
    permissions: List[str] = []  # subset of permissions.PERMISSION_CHOICES keys
    login_slug: Optional[str] = None
    telegram_id: Optional[int] = None
    # If set, this admin's effective permissions come from the group
    # instead of the `permissions` list above (see permissions.effective_permissions).
    group_id: Optional[int] = None
    # Optional "اعتبار پایه" - starting wholesale credit balance given to
    # this reseller right when they're created. Recorded as the first
    # AdminBalanceLog entry (note="اعتبار پایه اولیه"). 0/None = starts
    # with no credit, same as before this field existed.
    initial_balance: Optional[int] = None
    # "flat" (default) or "usage" - see models.AdminUser.billing_mode.
    billing_mode: Optional[str] = None
    # Starting GB volume pool, only meaningful when billing_mode="usage".
    # Recorded as the first AdminVolumeLog entry, same pattern as
    # initial_balance above.
    initial_volume_gb: Optional[float] = None
    # Superadmin-only (silently ignored for a level-2 Admin creating their
    # own Seller, who has no say in this - see routers/admins.py's
    # create_admin): lets a superadmin create a brand-new account directly
    # AS a level-3 Seller under a chosen existing level-2 Admin, instead of
    # always landing as a fresh level-2 Admin. None (the default) means
    # "level-2 Admin", same as every account created before this field
    # existed.
    parent_admin_id: Optional[int] = None


class AdminReparentRequest(BaseModel):
    """Superadmin-only (see routers/admins.py's reparent_admin): moves an
    EXISTING account between tiers/parents - None = promote to (or keep
    as) a level-2 Admin, or a level-2 Admin's id = make/move this account
    into a level-3 Seller under that Admin. Needed because the accounts
    created before this 3-tier feature existed were auto-migrated as
    level-2 Admins with no way to reclassify them as someone's Seller
    afterward - see hierarchy.py's docstrings for the fixed-3-levels rule
    this endpoint still has to respect."""
    parent_admin_id: Optional[int] = None


class AdminUpdate(BaseModel):
    password: Optional[str] = None  # if set, resets the password
    permissions: Optional[List[str]] = None
    login_slug: Optional[str] = None
    # Set (absolute value, not a delta) by a superadmin to top up this
    # admin's wholesale credit balance from the "مدیریت ادمین‌ها" page.
    balance: Optional[int] = None
    # Numeric Telegram id letting this admin manage their own group's users
    # directly from the bot - see telegram_bot/admin_scope.py.
    telegram_id: Optional[int] = None
    # Assign/unassign a permission group. Sending 0 clears it (falls back
    # to the individual `permissions` checkboxes) - same 0-means-clear
    # convention used elsewhere in this schema/router.
    group_id: Optional[int] = None
    # "flat" or "usage" - see models.AdminUser.billing_mode. Switching an
    # admin between modes does NOT convert/transfer any existing
    # balance/volume_balance_gb - each pool is simply ignored while the
    # other mode is active.
    billing_mode: Optional[str] = None


class AdminOut(BaseModel):
    id: int
    username: str
    is_superadmin: bool
    permissions: List[str] = []
    login_slug: Optional[str] = None
    balance: int = 0
    telegram_id: Optional[int] = None
    created_at: dt.datetime
    users_count: int = 0
    group_id: Optional[int] = None
    group_name: Optional[str] = None
    billing_mode: str = "flat"
    volume_balance_gb: Optional[float] = None
    # 3-tier hierarchy (see services/hierarchy.py) - "superadmin"/"admin"/"seller".
    role: str = "admin"
    parent_admin_id: Optional[int] = None
    parent_admin_username: Optional[str] = None
    # Node ids explicitly granted to this admin (see models.AdminNodeAccess) -
    # only ever meaningful for role="admin"; always empty for superadmin
    # (unrestricted - doesn't need a grant list) and seller (no direct node
    # access at all, see hierarchy.accessible_node_ids).
    accessible_node_ids: List[int] = []


class AdminNodeAccessUpdate(BaseModel):
    """Full-replace list of node ids granted to a level-2 Admin - superadmin
    only (see routers/admins.py's set_admin_nodes)."""
    node_ids: List[int] = []


class AdminGroupCreate(BaseModel):
    name: str
    permissions: List[str] = []


class AdminGroupUpdate(BaseModel):
    name: Optional[str] = None
    permissions: Optional[List[str]] = None


class AdminGroupOut(BaseModel):
    id: int
    name: str
    permissions: List[str] = []
    admins_count: int = 0


class AdminTopupRequest(BaseModel):
    # Signed delta - positive to top up, negative for a manual correction.
    amount: int
    note: Optional[str] = None


class AdminBalanceLogOut(BaseModel):
    id: int
    admin_id: int
    amount: int
    balance_after: int
    note: Optional[str] = None
    created_by_username: Optional[str] = None
    created_at: dt.datetime


class AdminVolumeTopupRequest(BaseModel):
    # Signed delta in GB - positive to top up, negative for a manual correction.
    amount_gb: float
    note: Optional[str] = None


class AdminVolumeLogOut(BaseModel):
    id: int
    admin_id: int
    amount_gb: float
    balance_after_gb: float
    note: Optional[str] = None
    created_by_username: Optional[str] = None
    created_at: dt.datetime


class AdminLoginLogOut(BaseModel):
    id: int
    admin_id: Optional[int] = None
    admin_username: Optional[str] = None
    attempted_username: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    success: bool
    created_at: dt.datetime


# ---------- RADIUS concurrent-session-limit reject/ban history ----------
class RadiusLimitEventLogOut(BaseModel):
    id: int
    connection_id: Optional[int] = None
    user_id: Optional[int] = None
    username: Optional[str] = None
    connection_type: Optional[str] = None
    event_type: str
    active_count: Optional[int] = None
    limit_value: Optional[int] = None
    banned_until: Optional[dt.datetime] = None
    client_ip: Optional[str] = None
    created_at: dt.datetime


# ---------- Bulk messaging ----------
class BroadcastRequest(BaseModel):
    text: str


class BroadcastResult(BaseModel):
    sent: int
    failed: int
    total: int
