import datetime as dt
from typing import Optional, List

from pydantic import BaseModel, ConfigDict, field_validator

from .models import NodeType, ConnectionType, UserStatus

_PORT_FIELDS = (
    "mt_port", "mt_api_ssl_port", "mt_endpoint_port", "mt_ovpn_port",
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


class ConnectionCreateOpenvpn(BaseModel):
    node_id: int
    max_concurrent_sessions: Optional[int] = 1  # 0 = unlimited


class ConnectionCreateL2tp(BaseModel):
    node_id: int
    max_concurrent_sessions: Optional[int] = 1  # 0 = unlimited


class ConnectionCreateIkev2(BaseModel):
    node_id: int
    max_concurrent_sessions: Optional[int] = 1  # 0 = unlimited


class ConnectionCreateXray(BaseModel):
    node_id: int
    flow: Optional[str] = ""


class ConnectionUpdate(BaseModel):
    enabled: Optional[bool] = None
    max_concurrent_sessions: Optional[int] = None
    banned_until: Optional[dt.datetime] = None  # set to null/past to unban


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
    last_rx_bytes: int = 0
    last_tx_bytes: int = 0
    total_bytes: int = 0
    created_at: dt.datetime
    # See models.Connection.purchase_batch/package_name_snapshot - used to
    # group a user's connections by purchase in the bot's "اکانت من" screen.
    purchase_batch: Optional[str] = None
    package_name_snapshot: Optional[str] = None


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
    # bundled into the package is provisioned automatically.
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
    enabled: bool = True
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


# ---------- Panel-wide settings (payment info shown by the sales bot) ----------
class PanelSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    payment_card_number: Optional[str] = None
    payment_card_holder: Optional[str] = None
    payment_instructions: Optional[str] = None
    topup_presets: Optional[str] = ""


class PanelSettingsUpdate(BaseModel):
    payment_card_number: Optional[str] = None
    payment_card_holder: Optional[str] = None
    payment_instructions: Optional[str] = None
    topup_presets: Optional[str] = None


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


class BotSettingsUpdate(BaseModel):
    bot_token: Optional[str] = None
    admin_ids: Optional[str] = None
    approval_chat_ids: Optional[str] = None
    enabled: Optional[bool] = None
    customer_bot_enabled: Optional[bool] = None


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


class BotRenewRequest(BaseModel):
    add_gb: float = 0
    add_days: int = 0
    reset_usage: bool = False


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


# ---------- Admin management (superadmin only) ----------
class AdminCreate(BaseModel):
    username: str
    password: str
    permissions: List[str] = []  # subset of permissions.PERMISSION_CHOICES keys
    login_slug: Optional[str] = None
    telegram_id: Optional[int] = None


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


# ---------- Bulk messaging ----------
class BroadcastRequest(BaseModel):
    text: str


class BroadcastResult(BaseModel):
    sent: int
    failed: int
    total: int
